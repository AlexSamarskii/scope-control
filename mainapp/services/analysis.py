"""Tenant-aware analysis transitions. PostgreSQL records own delivery state.

Retains SpeedPy Celery and its per-record transaction principle; tenant lookup
continues the Apptension principle already adapted in S1. No second queue/ORM.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone
import hashlib
import json
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone
import structlog

from mainapp.models import Team
from mainapp.models.analysis import (
    AnalysisAttempt,
    AnalysisRun,
    ClientRequest,
    _domain_write,
)
from mainapp.models.scope_control import ScopeVersion, Project
from mainapp.models.data_lifecycle import DeletedAnalysisUsage
from mainapp.middleware_recovery import require_live, is_quarantined
from mainapp.services.analysis_backends import TransientAnalysisError, get_backend
from mainapp.services.analysis_validation import InvalidAnalysis, validate_response
from mainapp.services.analysis_provider import ProviderFailure, ProviderReply
from mainapp.services import analysis_budget as money
from mainapp.services.scope_control import membership_for

logger = structlog.get_logger(__name__)
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3


class AnalysisConflict(ValidationError):
    pass


class AnalysisQuota(ValidationError):
    pass


def _key(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Некорректный ключ запроса.") from None


def _lock_team(user, team_id):
    require_live()
    team = Team.objects.select_for_update().filter(pk=team_id, is_active=True).first()
    if team is None:
        raise Http404("Компания не найдена.")
    membership_for(user, team.pk, write=True, lock=True)
    return team


@transaction.atomic
def create_request(
    user,
    team_id,
    project_id,
    *,
    version_id,
    body,
    source_description="",
    submission_key,
):
    _lock_team(user, team_id)
    version = ScopeVersion.objects.filter(
        pk=version_id,
        project_id=project_id,
        project__team_id=team_id,
        project__deletion_requested_at__isnull=True,
    ).first()
    if version is None:
        raise Http404("Версия не найдена.")
    key = _key(submission_key)
    existing = ClientRequest.objects.filter(
        scope_version=version, created_by=user, submission_key=key
    ).first()
    if existing:
        if existing.body != body or existing.source_description != source_description:
            raise AnalysisConflict("Этот ключ уже использован для другого текста.")
        return existing
    item = ClientRequest(
        scope_version=version,
        created_by=user,
        submission_key=key,
        body=body,
        source_description=source_description,
    )
    with _domain_write():
        item.save(force_insert=True)
    return item


def build_input(run):
    item = run.client_request
    version = item.scope_version
    packet = {
        "request_id": str(item.pk),
        "scope_version_id": str(version.pk),
        "backend": run.backend,
        "schema_version": run.schema_version,
        "fixture_revision": run.fixture_revision,
        "sources": {
            f"scope:{version.pk}:body": version.body,
            f"scope:{version.pk}:exclusions": version.exclusions,
            f"scope:{version.pk}:revision_rules": version.revision_rules,
            f"request:{item.pk}:body": item.body,
        },
    }
    if run.backend == "yandex":
        packet["provider_profile"] = run.provider_profile
    return packet


def input_hash(packet):
    data = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@transaction.atomic
def start_analysis(
    user,
    team_id,
    project_id,
    request_id,
    *,
    start_key,
    external_processing_confirmed=False,
):
    _lock_team(user, team_id)
    item = (
        ClientRequest.objects.select_related("scope_version")
        .filter(
            pk=request_id,
            scope_version__project_id=project_id,
            scope_version__project__team_id=team_id,
            scope_version__project__deletion_requested_at__isnull=True,
        )
        .first()
    )
    if item is None:
        raise Http404("Запрос не найден.")
    key = _key(start_key)
    existing = AnalysisRun.objects.filter(
        client_request=item, requested_by=user, start_key=key
    ).first()
    if existing:
        return existing
    get_backend()  # Closed registry; no run when disabled/unknown.
    if AnalysisRun.objects.filter(
        client_request=item, status__in=["queued", "running"]
    ).exists():
        raise AnalysisConflict("Этот запрос уже обрабатывается.")
    recent = AnalysisRun.objects.filter(
        client_request__scope_version__project__team_id=team_id,
        created_at__gt=timezone.now() - timedelta(hours=1),
    ).count()
    recent += DeletedAnalysisUsage.objects.filter(
        team_uuid=team_id,
        kind="run",
        occurred_at__gt=timezone.now() - timedelta(hours=1),
    ).count()
    if recent >= settings.SCOPE_ANALYSIS_RUNS_PER_HOUR:
        raise AnalysisQuota("Достигнут часовой лимит запусков компании.")
    run = AnalysisRun(client_request=item, requested_by=user, start_key=key)
    if settings.SCOPE_ANALYSIS_BACKEND == "yandex":
        if external_processing_confirmed is not True:
            raise ValidationError(
                "Подтвердите синтетический характер данных и отправку в Yandex AI Studio."
            )
        from mainapp.services.analysis_provider import public_profile

        run.backend = "yandex"
        run.provider_profile = public_profile()
        run.external_processing_confirmed_at = timezone.now()
    run.input_sha256 = input_hash(build_input(run))
    with _domain_write():
        run.save(force_insert=True)
    transaction.on_commit(lambda: publish_run(str(run.pk)))
    return run


def publish_run(run_id):
    """A failed notification never removes the committed queued record."""
    if (
        is_quarantined()
        or not AnalysisRun.objects.filter(
            pk=run_id,
            client_request__scope_version__project__deletion_requested_at__isnull=True,
        ).exists()
    ):
        return False
    from mainapp.tasks.analysis import run_scope_analysis

    try:
        with run_scope_analysis.app.connection_for_write(
            connect_timeout=3,
            transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
        ) as connection:
            # Establish once explicitly; Redis socket deadlines are set above
            # because the general connect_timeout alone did not bound this path.
            connection.ensure_connection(max_retries=0)
            run_scope_analysis.apply_async(
                args=[str(run_id)],
                connection=connection,
                retry=False,
                ignore_result=True,
                argsrepr="(<run_uuid>,)",
            )
        return True
    except Exception:
        logger.warning("scope_analysis_publish_failed", run_id=str(run_id))
        return False


@contextmanager
def _locked_run(run_id):
    """Consistent lock order: Team, Membership, Run, then Attempt."""
    with transaction.atomic():
        if is_quarantined():
            yield None, False
            return
        hint = (
            AnalysisRun.objects.select_related(
                "client_request__scope_version__project", "requested_by"
            )
            .filter(pk=run_id)
            .first()
        )
        if hint is None:
            yield None, False
            return
        if hint.backend == "yandex":
            money.lock_budget()
        team_id = hint.client_request.scope_version.project.team_id
        Team.objects.select_for_update().get(pk=team_id)
        hint.requested_by.refresh_from_db(fields=["is_active"])
        try:
            membership_for(hint.requested_by, team_id, write=True, lock=True)
            authorized = True
        except (Http404, PermissionDenied):
            authorized = False
        project = (
            Project.all_objects.select_for_update()
            .filter(pk=hint.client_request.scope_version.project_id)
            .first()
        )
        authorized = (
            authorized and project is not None and project.deletion_requested_at is None
        )
        run = AnalysisRun.objects.select_for_update().filter(pk=run_id).first()
        yield run, authorized


def _save_run(run, *, status, error_code="", result=None, now=None):
    now = now or timezone.now()
    run.status = status
    run.error_code = error_code
    run.validated_result = result
    run.lease_token = None
    run.lease_expires_at = None
    run.finished_at = now if run.is_terminal else None
    with _domain_write():
        run.save()


def _close_attempt(run, outcome, now, *, error_code=None):
    attempt = run.attempts.select_for_update().get(lease_token=run.lease_token)
    attempt.outcome = outcome
    attempt.error_code = (
        error_code
        if error_code is not None
        else ("" if outcome == "validated" else outcome)
    )
    attempt.finished_at = now
    with _domain_write():
        attempt.save()
    if run.backend == "yandex":
        money.finish_unknown(attempt, now)


def _recover_locked(run, now):
    if run.status != "running" or run.lease_expires_at > now:
        return False
    _close_attempt(run, "worker_lost", now)
    if run.attempt_count >= MAX_ATTEMPTS:
        _save_run(run, status="failed", error_code="attempts_exhausted", now=now)
    else:
        run.next_attempt_at = now
        _save_run(run, status="queued", error_code="worker_lost", now=now)
    return True


@dataclass(frozen=True)
class Claim:
    run_id: str
    token: UUID
    packet: dict


def claim_run(run_id):
    with _locked_run(run_id) as (run, authorized):
        if run is None or run.is_terminal:
            return None
        now = timezone.now()
        if not authorized:
            if run.status == "running":
                _close_attempt(run, "access_revoked", now)
            _save_run(run, status="failed", error_code="access_revoked", now=now)
            return None
        if run.status == "running":
            _recover_locked(run, now)
            if run.status == "running" or run.is_terminal:
                return None
        if not authorized:
            _save_run(run, status="failed", error_code="access_revoked", now=now)
            return None
        if run.next_attempt_at > now:
            return None
        packet = build_input(run)
        if input_hash(packet) != run.input_sha256:
            _save_run(run, status="failed", error_code="input_changed", now=now)
            return None
        try:
            get_backend(run)
        except (ValidationError, ProviderFailure):
            code = (
                "configuration_changed"
                if run.backend == "yandex"
                else "backend_disabled"
            )
            _save_run(run, status="failed", error_code=code, now=now)
            return None
        midnight = now.astimezone(dt_timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        used = AnalysisAttempt.objects.filter(
            run__client_request__scope_version__project__team_id=run.client_request.scope_version.project.team_id,
            started_at__gte=midnight,
        ).count()
        used += DeletedAnalysisUsage.objects.filter(
            team_uuid=run.client_request.scope_version.project.team_id,
            kind="attempt",
            occurred_at__gte=midnight,
        ).count()
        if used >= settings.SCOPE_ANALYSIS_ATTEMPTS_PER_DAY:
            _save_run(run, status="failed", error_code="quota_exceeded", now=now)
            return None
        if run.attempt_count >= MAX_ATTEMPTS:
            _save_run(run, status="failed", error_code="attempts_exhausted", now=now)
            return None
        if run.backend == "yandex" and not money.can_reserve(
            run.client_request.scope_version.project.team_id, now
        ):
            _save_run(run, status="failed", error_code="budget_exceeded", now=now)
            return None
        run.attempt_count += 1
        run.status = "running"
        run.error_code = ""
        run.started_at = run.started_at or now
        run.lease_token = uuid4()
        run.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        with _domain_write():
            run.save()
            attempt = AnalysisAttempt.objects.create(
                run=run,
                number=run.attempt_count,
                lease_token=run.lease_token,
                started_at=now,
            )
            if run.backend == "yandex":
                money.reserve_attempt(attempt)
        return Claim(str(run.pk), run.lease_token, packet)


def finish_run(run_id, token, *, raw=None, error=None, transient=False):
    with _locked_run(run_id) as (run, authorized):
        now = timezone.now()
        if (
            run is None
            or run.status != "running"
            or run.lease_token != token
            or run.lease_expires_at <= now
        ):
            return False
        outcome, result = error, None
        if not authorized:
            outcome = "access_revoked"
        elif input_hash(build_input(run)) != run.input_sha256:
            outcome = "internal_error"
        elif error is None:
            try:
                result = validate_response(raw, build_input(run)["sources"])
                outcome = "validated"
            except InvalidAnalysis:
                outcome = "invalid_response"
        allowed_errors = {
            "access_revoked",
            "invalid_response",
            "transient_error",
            "internal_error",
            "configuration_missing",
            "configuration_changed",
            "provider_auth",
            "provider_rate_limit",
            "provider_unavailable",
            "provider_timeout",
            "provider_connection",
            "provider_transport",
            "provider_protocol",
            "provider_rejected",
            "provider_refusal",
            "provider_truncated",
            "provider_usage_anomaly",
            "input_too_large",
            "response_too_large",
            "dispatch_denied",
        }
        if outcome != "validated" and outcome not in allowed_errors:
            outcome = "internal_error"
        retryable = outcome == "transient_error" or (
            transient
            and outcome
            in {
                "provider_rate_limit",
                "provider_unavailable",
                "provider_timeout",
                "provider_connection",
                "provider_transport",
            }
        )
        attempt_outcome = (
            outcome
            if outcome
            in {"validated", "access_revoked", "invalid_response", "transient_error"}
            else ("transient_error" if retryable else "internal_error")
        )
        _close_attempt(
            run,
            attempt_outcome,
            now,
            error_code="" if outcome == "validated" else outcome,
        )
        if outcome == "validated":
            _save_run(run, status="validated", result=result, now=now)
        elif retryable and run.attempt_count < MAX_ATTEMPTS:
            run.next_attempt_at = now + timedelta(
                seconds=10 if run.attempt_count == 1 else 60
            )
            _save_run(run, status="queued", error_code=outcome, now=now)
        else:
            _save_run(run, status="failed", error_code=outcome, now=now)
        logger.info(
            "scope_analysis_finished",
            run_id=str(run.pk),
            code=run.status,
            attempts=run.attempt_count,
        )
        return True


def mark_generation(run_id, token):
    with _locked_run(run_id) as (run, authorized):
        now = timezone.now()
        if (
            run is None
            or run.backend != "yandex"
            or not authorized
            or run.status != "running"
            or run.lease_token != token
            or run.lease_expires_at <= now
        ):
            return False
        try:
            get_backend(run)
        except (ValidationError, ProviderFailure):
            return False
        budget = money.ProviderBudget.objects.get(key=money.BUDGET_KEY)
        if budget.suspended or input_hash(build_input(run)) != run.input_sha256:
            return False
        attempt = run.attempts.select_for_update().get(lease_token=token)
        return money.mark_dispatched(attempt, now)


def record_receipt(run_id, token, reply):
    # Financial reconciliation may be late; it never changes domain attempt/result.
    with _locked_run(run_id) as (run, authorized):
        if run is None or run.backend != "yandex":
            return False
        attempt = run.attempts.select_for_update().filter(lease_token=token).first()
        if attempt is None:
            return False
        return money.settle_receipt(attempt, reply, timezone.now())


def execute_run(run_id, *, backend=None):
    """Test backend injection is kept for legacy fixture; Yandex uses its protocol."""
    claim = claim_run(run_id)
    if claim is None:
        return
    try:
        if claim.packet.get("provider_profile"):
            run = AnalysisRun.objects.get(pk=run_id)
            reply = get_backend(
                run, before_generate=lambda: mark_generation(run_id, claim.token)
            )(claim.packet)
            if not isinstance(reply, ProviderReply):
                raise ProviderFailure("provider_protocol")
            settled = record_receipt(run_id, claim.token, reply)
            if reply.anomaly or not settled:
                finish_run(run_id, claim.token, error="provider_usage_anomaly")
            elif reply.error:
                finish_run(run_id, claim.token, error=reply.error)
            else:
                finish_run(run_id, claim.token, raw=reply.raw)
            return
        raw = (backend or get_backend())(claim.packet)
    except ProviderFailure as exc:
        finish_run(run_id, claim.token, error=exc.code, transient=exc.transient)
    except ValidationError:
        finish_run(run_id, claim.token, error="configuration_changed")
    except TransientAnalysisError:
        finish_run(run_id, claim.token, error="transient_error")
    except Exception:
        finish_run(run_id, claim.token, error="internal_error")
    else:
        finish_run(run_id, claim.token, raw=raw)


def reconcile_analysis(*, limit=100):
    require_live()
    now = timezone.now()
    ids = list(
        AnalysisRun.objects.filter(
            Q(status="queued", next_attempt_at__lte=now)
            | Q(status="running", lease_expires_at__lte=now)
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[: min(max(limit, 1), 100)]
    )
    published = recovered = 0
    for run_id in ids:
        due = False
        with _locked_run(run_id) as (run, authorized):
            if run is None or run.is_terminal:
                continue
            if not authorized:
                if run.status == "running":
                    _close_attempt(run, "access_revoked", timezone.now())
                _save_run(run, status="failed", error_code="access_revoked")
                continue
            recovered += _recover_locked(run, timezone.now())
            if run.is_terminal:
                continue
            if not authorized and run.status == "queued":
                _save_run(run, status="failed", error_code="access_revoked")
                continue
            due = run.status == "queued" and run.next_attempt_at <= timezone.now()
        if due:
            published += publish_run(str(run_id))
    return {"scanned": len(ids), "recovered": recovered, "published": published}
