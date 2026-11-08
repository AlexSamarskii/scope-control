"""Human-only transitions. Locks: Team → Membership → Project → ChangeCard.

Scoped lookups extend the credited Apptension principle; domain event context
extends Next.js activityLogs (MIT). No model or broker is called here.
"""

from contextlib import contextmanager
from datetime import datetime, timezone as utc
from decimal import Decimal, InvalidOperation
import hashlib
import json
from uuid import UUID

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from mainapp.models import Team
from mainapp.models.analysis import ClientRequest, _domain_write
from mainapp.models.change_control import ChangeCard, ChangeRevision, ChangeEvent
from mainapp.models.scope_control import Project, ScopeVersion
from mainapp.services.scope_control import membership_for, confirm_scope


class ChangeConflict(ValidationError):
    pass


def _uuid(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Некорректный ключ действия.") from None


def _amount(value):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, bool):
            raise InvalidOperation()
        amount = Decimal(str(value))
        if (
            not amount.is_finite()
            or amount < 0
            or amount > Decimal("9999999999.99")
            or amount.as_tuple().exponent < -2
        ):
            raise InvalidOperation()
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(
            "Укажите неотрицательную сумму с максимум двумя знаками."
        ) from None


def _payload(action, **data):
    def default(value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValidationError("Дата ответа должна содержать часовой пояс.")
            return value.astimezone(utc.utc).isoformat()
        return str(value)

    raw = json.dumps(
        {"action": action, **data},
        default=default,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _confirmed(value):
    if value is not True:
        raise ValidationError("Подтвердите явное действие менеджера.")


def get_project(user, team_id, project_id):
    membership_for(user, team_id)
    project = Project.objects.filter(pk=project_id, team_id=team_id).first()
    if project is None:
        raise Http404("Проект не найден.")
    return project


def get_card(user, team_id, project_id, card_id):
    get_project(user, team_id, project_id)
    card = (
        ChangeCard.objects.select_related("client_request__scope_version__project")
        .filter(
            pk=card_id,
            client_request__scope_version__project_id=project_id,
            client_request__scope_version__project__team_id=team_id,
        )
        .first()
    )
    if card is None:
        raise Http404("Карточка не найдена.")
    return card


@contextmanager
def _locked(user, team_id, project_id, card_id=None):
    with transaction.atomic():
        team = (
            Team.objects.select_for_update().filter(pk=team_id, is_active=True).first()
        )
        if team is None or not user.is_authenticated:
            raise Http404("Компания не найдена.")
        user.refresh_from_db(fields=["is_active"])
        membership_for(user, team_id, write=True, lock=True)
        project = (
            Project.objects.select_for_update()
            .filter(pk=project_id, team_id=team_id)
            .first()
        )
        if project is None:
            raise Http404("Проект не найден.")
        card = None
        if card_id is not None:
            card = (
                ChangeCard.objects.select_for_update()
                .filter(pk=card_id, client_request__scope_version__project=project)
                .first()
            )
            if card is None:
                raise Http404("Карточка не найдена.")
        yield project, card


def _expect(card, expected_revision_id, expected_event_id):
    revision = card.current_revision
    event = revision.last_event
    if _uuid(expected_revision_id) != revision.pk or _uuid(
        expected_event_id, optional=True
    ) != (event.pk if event else None):
        raise ChangeConflict("Решение уже изменилось. Откройте карточку заново.")
    return revision, event


def _replay(query, payload):
    existing = query.first()
    if existing and existing.payload_sha256 != payload:
        raise ChangeConflict("Ключ уже использован для другого действия.")
    return existing


def _revision_data(
    category, description, rationale, amount, currency, deadline_effect, analysis_run_id
):
    return dict(
        category=category,
        description=description,
        rationale=rationale,
        amount=_amount(amount),
        currency=currency,
        deadline_effect=deadline_effect,
        analysis_run_id=_uuid(analysis_run_id, optional=True),
    )


def record_decision(
    user,
    team_id,
    project_id,
    request_id,
    *,
    category,
    description,
    rationale,
    submission_key,
    confirmed=False,
    amount=None,
    currency="",
    deadline_effect="",
    analysis_run_id=None
):
    _confirmed(confirmed)
    key = _uuid(submission_key)
    data = _revision_data(
        category,
        description,
        rationale,
        amount,
        currency,
        deadline_effect,
        analysis_run_id,
    )
    payload = _payload("create", request_id=_uuid(request_id), **data)
    with _locked(user, team_id, project_id) as (project, _):
        item = ClientRequest.objects.filter(
            pk=request_id, scope_version__project=project
        ).first()
        if item is None:
            raise Http404("Запрос не найден.")
        card = ChangeCard.objects.filter(client_request=item).first()
        if card:
            replay = _replay(
                card.revisions.filter(created_by=user, submission_key=key), payload
            )
            if replay:
                return replay
            raise ChangeConflict("Решение по этому запросу уже существует.")
        with _domain_write():
            card = ChangeCard.objects.create(client_request=item, created_by=user)
            revision = ChangeRevision.objects.create(
                card=card,
                number=1,
                created_by=user,
                submission_key=key,
                payload_sha256=payload,
                **data
            )
        return revision


def revise_decision(
    user,
    team_id,
    project_id,
    card_id,
    *,
    expected_revision_id,
    expected_event_id=None,
    category,
    description,
    rationale,
    submission_key,
    confirmed=False,
    amount=None,
    currency="",
    deadline_effect="",
    analysis_run_id=None
):
    _confirmed(confirmed)
    key = _uuid(submission_key)
    data = _revision_data(
        category,
        description,
        rationale,
        amount,
        currency,
        deadline_effect,
        analysis_run_id,
    )
    payload = _payload(
        "revise",
        expected_revision_id=_uuid(expected_revision_id),
        expected_event_id=_uuid(expected_event_id, optional=True),
        **data
    )
    with _locked(user, team_id, project_id, card_id) as (_, card):
        replay = _replay(
            card.revisions.filter(created_by=user, submission_key=key), payload
        )
        if replay:
            return replay
        previous, _ = _expect(card, expected_revision_id, expected_event_id)
        with _domain_write():
            return ChangeRevision.objects.create(
                card=card,
                number=previous.number + 1,
                previous_revision=previous,
                created_by=user,
                submission_key=key,
                payload_sha256=payload,
                **data
            )


def _event(card, revision, user, key, payload, kind, **data):
    last = revision.last_event
    with _domain_write():
        return ChangeEvent.objects.create(
            card=card,
            revision=revision,
            number=last.number + 1 if last else 1,
            kind=kind,
            created_by=user,
            submission_key=key,
            payload_sha256=payload,
            **data
        )


def record_shared(
    user,
    team_id,
    project_id,
    card_id,
    *,
    expected_revision_id,
    expected_event_id=None,
    submission_key,
    confirmed=False
):
    return _response(
        user,
        team_id,
        project_id,
        card_id,
        kind="shared",
        expected_revision_id=expected_revision_id,
        expected_event_id=expected_event_id,
        submission_key=submission_key,
        confirmed=confirmed,
    )


def record_client_response(
    user,
    team_id,
    project_id,
    card_id,
    *,
    kind,
    sender_name,
    occurred_at,
    evidence_text,
    source_description="",
    expected_revision_id,
    expected_event_id=None,
    submission_key,
    confirmed=False
):
    if kind not in ("accepted", "rejected"):
        raise ValidationError("Выберите принятие или отказ.")
    return _response(
        user,
        team_id,
        project_id,
        card_id,
        kind=kind,
        expected_revision_id=expected_revision_id,
        expected_event_id=expected_event_id,
        submission_key=submission_key,
        confirmed=confirmed,
        sender_name=sender_name,
        occurred_at=occurred_at,
        evidence_text=evidence_text,
        source_description=source_description,
    )


def _response(
    user,
    team_id,
    project_id,
    card_id,
    *,
    kind,
    expected_revision_id,
    expected_event_id,
    submission_key,
    confirmed,
    **data
):
    _confirmed(confirmed)
    key = _uuid(submission_key)
    payload = _payload(
        kind,
        expected_revision_id=_uuid(expected_revision_id),
        expected_event_id=_uuid(expected_event_id, optional=True),
        **data
    )
    with _locked(user, team_id, project_id, card_id) as (_, card):
        replay = _replay(
            card.events.filter(created_by=user, submission_key=key), payload
        )
        if replay:
            return replay
        revision, _ = _expect(card, expected_revision_id, expected_event_id)
        return _event(card, revision, user, key, payload, kind, **data)


def _apply_payload(
    expected_revision_id,
    expected_event_id,
    expected_version,
    body,
    exclusions,
    revision_rules,
):
    return _payload(
        "apply",
        expected_revision_id=_uuid(expected_revision_id),
        expected_event_id=_uuid(expected_event_id),
        expected_version=_uuid(expected_version),
        body=body,
        exclusions=exclusions,
        revision_rules=revision_rules,
    )


def _scope_ready(project, card, event, expected_version):
    current = project.versions.first()
    if event is None or event.kind != "accepted":
        raise ChangeConflict("Сначала запишите принятие актуальной редакции.")
    if (
        current.pk != _uuid(expected_version)
        or current.pk != card.client_request.scope_version_id
    ):
        raise ChangeConflict(
            "Исходный объём устарел. Создайте запрос по актуальной версии и проверьте решение заново."
        )
    return current


def _preview_identity(user, team_id, project_id, card_id, payload):
    return {
        "actor": str(user.pk),
        "team": str(team_id),
        "project": str(project_id),
        "card": str(card_id),
        "payload": payload,
    }


def preview_scope_change(
    user,
    team_id,
    project_id,
    card_id,
    *,
    expected_revision_id,
    expected_event_id,
    expected_version,
    body,
    exclusions="",
    revision_rules="",
    submission_key
):
    _uuid(submission_key)
    payload = _apply_payload(
        expected_revision_id,
        expected_event_id,
        expected_version,
        body,
        exclusions,
        revision_rules,
    )
    with _locked(user, team_id, project_id, card_id) as (project, card):
        _, event = _expect(card, expected_revision_id, expected_event_id)
        previous = _scope_ready(project, card, event, expected_version)
        ScopeVersion(
            project=project,
            number=previous.number + 1,
            previous_version=previous,
            confirmed_by=user,
            body=body,
            exclusions=exclusions,
            revision_rules=revision_rules,
        ).full_clean()
        return signing.dumps(
            _preview_identity(user, team_id, project_id, card_id, payload),
            salt="scope-change-preview-v1",
        )


def apply_scope_change(
    user,
    team_id,
    project_id,
    card_id,
    *,
    expected_revision_id,
    expected_event_id,
    expected_version,
    body,
    exclusions="",
    revision_rules="",
    submission_key,
    confirmed=False,
    preview_token=""
):
    _confirmed(confirmed)
    key = _uuid(submission_key)
    payload = _apply_payload(
        expected_revision_id,
        expected_event_id,
        expected_version,
        body,
        exclusions,
        revision_rules,
    )
    with _locked(user, team_id, project_id, card_id) as (project, card):
        replay = _replay(
            card.events.filter(created_by=user, submission_key=key), payload
        )
        if replay:
            return replay.resulting_scope_version
        try:
            receipt = signing.loads(
                preview_token, salt="scope-change-preview-v1", max_age=1800
            )
        except (signing.BadSignature, TypeError):
            raise ValidationError(
                "Сначала откройте предпросмотр текущего текста (действует 30 минут)."
            ) from None
        if receipt != _preview_identity(user, team_id, project_id, card_id, payload):
            raise ValidationError(
                "Текст или реквизиты изменились. Повторите предпросмотр."
            )
        revision, event = _expect(card, expected_revision_id, expected_event_id)
        _scope_ready(project, card, event, expected_version)
        version = confirm_scope(
            user,
            team_id,
            project_id,
            expected_version=expected_version,
            body=body,
            exclusions=exclusions,
            revision_rules=revision_rules,
            confirmed=True,
        )
        _event(
            card,
            revision,
            user,
            key,
            payload,
            "applied",
            resulting_scope_version=version,
        )
        return version
