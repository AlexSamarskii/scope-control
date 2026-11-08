"""On-demand, tenant-scoped text exports; no files, broker, model, or cache."""

import csv
from io import StringIO

from django.core.exceptions import ValidationError
from django.db.models import CharField, F, OuterRef, Subquery
from django.db.models.functions import Cast, JSONObject

from mainapp.models.change_control import (
    ChangeCard,
    ChangeRevision,
    ChangeEvent,
    CATEGORY_LABELS,
    STATE_LABELS,
)
from mainapp.services.change_control import ChangeConflict, _uuid, get_card, get_project


def build_draft(revision):
    """Internal pure renderer; callers authorize the selected immutable revision."""
    item = revision.card.client_request
    return (
        f"Предлагаю согласовать изменение по проекту «{item.scope_version.project.name}».\n\n"
        f"{revision.description}\n\nСогласованный объём: версия {item.scope_version.number}.\n"
        f"Основание: {revision.rationale}\n"
        f"Стоимость изменения: {revision.amount_label}.\n"
        f"Влияние на срок: {revision.deadline_effect or 'Нужно уточнить'}.\n\n"
        "Пожалуйста, подтвердите эти условия или укажите замечания.\n"
    )


def draft_text(user, team_id, project_id, card_id, revision_id):
    card = get_card(user, team_id, project_id, card_id)
    revision = card.current_revision
    if revision is None or revision.pk != _uuid(revision_id):
        raise ChangeConflict("Редакция устарела. Откройте актуальную карточку.")
    return build_draft(revision)


def _current_cards(project_id):
    # One SQL statement supplies revision, status and latest answer from the same
    # PostgreSQL statement snapshot, including the answer after an applied event.
    events = ChangeEvent.objects.filter(revision_id=OuterRef("pk")).order_by("-number")
    last = events.annotate(
        document=JSONObject(
            kind=F("kind"),
            scope_version=F("resulting_scope_version__number"),
        )
    ).values("document")[:1]
    answer = (
        events.filter(kind__in=["accepted", "rejected"])
        .annotate(
            document=JSONObject(
                kind=F("kind"),
                actor=F("created_by__email"),
                recorded_at=F("created_at"),
                sender=F("sender_name"),
                occurred_at=F("occurred_at"),
                evidence=F("evidence_text"),
                source=F("source_description"),
            )
        )
        .values("document")[:1]
    )
    revision = (
        ChangeRevision.objects.filter(card_id=OuterRef("pk"))
        .order_by("-number")
        .annotate(
            document=JSONObject(
                id=F("pk"),
                number=F("number"),
                category=F("category"),
                description=F("description"),
                rationale=F("rationale"),
                amount=Cast("amount", CharField()),
                currency=F("currency"),
                deadline_effect=F("deadline_effect"),
                actor=F("created_by__email"),
                recorded_at=F("created_at"),
                event=Subquery(last),
                answer=Subquery(answer),
            )
        )
        .values("document")[:1]
    )
    return (
        ChangeCard.objects.filter(client_request__scope_version__project_id=project_id)
        .annotate(
            snapshot=Subquery(revision),
            scope_number=F("client_request__scope_version__number"),
        )
        .only("id", "client_request_id", "created_by_id", "created_at", "updated_at")
        .order_by("pk")
    )


def current_cards(user, team_id, project_id):
    get_project(user, team_id, project_id)
    return _current_cards(project_id)


def snapshot_state(snapshot):
    return (snapshot.get("event") or {}).get("kind") or (
        "draft" if snapshot["category"] == "possible_change" else "reviewed"
    )


def csv_text(user, team_id, project_id):
    cards = list(current_cards(user, team_id, project_id)[:1001])
    if len(cards) > 1000:
        raise ValidationError(
            "Выгрузка ограничена 1000 карточками проекта. Частичный реестр не сформирован."
        )
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "request_id",
            "card_id",
            "revision_id",
            "source_version",
            "category",
            "description",
            "rationale",
            "amount",
            "currency",
            "deadline_effect",
            "state",
            "decision_actor",
            "decision_at",
            "response_actor",
            "response_recorded_at",
            "sender",
            "response_occurred_at",
            "evidence",
            "source_description",
            "resulting_version",
        ]
    )

    # Protect EVERY user-controlled text cell, including whitespace/control/BOM
    # prefixes. CSV quoting alone does not prevent spreadsheet formula execution.
    def text_cell(value):
        return "'" + str(value or "")

    for card in cards:
        s = card.snapshot
        answer = s.get("answer") or {}
        writer.writerow(
            [
                str(card.client_request_id),
                str(card.pk),
                s["id"],
                card.scope_number,
                CATEGORY_LABELS[s["category"]],
                text_cell(s["description"]),
                text_cell(s["rationale"]),
                s["amount"] if s["amount"] is not None else "",
                s["currency"],
                text_cell(s["deadline_effect"]),
                STATE_LABELS[snapshot_state(s)],
                text_cell(s["actor"]),
                s["recorded_at"],
                text_cell(answer.get("actor")),
                answer.get("recorded_at", ""),
                text_cell(answer.get("sender")),
                answer.get("occurred_at", ""),
                text_cell(answer.get("evidence")),
                text_cell(answer.get("source")),
                (s.get("event") or {}).get("scope_version") or "",
            ]
        )
    return output.getvalue()
