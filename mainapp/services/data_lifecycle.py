"""Owner intent and operator-only project purge; ordinary immutable guards stay intact."""

from datetime import timedelta
import hashlib
import json
from uuid import UUID, uuid4

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections, transaction
from django.http import Http404
from django.utils import timezone

from mainapp.middleware_recovery import require_live
from mainapp.models import Team
from mainapp.models.analysis import _domain_write
from mainapp.models.data_lifecycle import DataLifecycleState, ProjectDeletion
from mainapp.models.scope_control import Project
from mainapp.services import analysis_budget as money
from mainapp.services.backup_storage import OperatorStorage, StorageFailure
from mainapp.services.scope_control import membership_for

SALT = "scope-project-delete-v1"


class DeletionConflict(ValidationError):
    pass


def installation(*, lock=False):
    if not DataLifecycleState.objects.filter(key="installation").exists():
        # Serialized by the wallet in all writers.
        with _domain_write():
            DataLifecycleState.objects.create(key="installation")
    query = DataLifecycleState.objects
    if lock:
        query = query.select_for_update()
    return query.get(key="installation")


def _owner(user, team_id, *, lock=False):
    membership = membership_for(user, team_id, lock=lock)
    if membership.role != "owner":
        raise PermissionDenied("Удаление доступно только владельцу компании.")
    return membership


def _payload(user, team_id, project):
    latest = project.versions.first()
    return dict(
        actor=str(user.pk),
        team=str(team_id),
        project=str(project.pk),
        name=project.name,
        version=str(latest.pk) if latest else "",
    )


def preview_deletion(user, team_id, project_id, *, name):
    _owner(user, team_id)
    project = Project.objects.filter(pk=project_id, team_id=team_id).first()
    if project is None:
        raise Http404("Проект не найден.")
    if name != project.name:
        raise ValidationError("Введите точное название проекта.")
    return signing.dumps(_payload(user, team_id, project), salt=SALT)


@transaction.atomic
def request_deletion(
    user, team_id, project_id, *, name, preview_token, submission_key, confirmed
):
    require_live()
    money.lock_budget()  # Always before team, including fixture/no-charge projects.
    Team.objects.select_for_update().get(pk=team_id)
    user.refresh_from_db(fields=["is_active"])
    _owner(user, team_id, lock=True)
    try:
        key = UUID(str(submission_key))
        payload = signing.loads(preview_token, salt=SALT, max_age=600)
    except (ValueError, TypeError, signing.BadSignature):
        raise DeletionConflict("Предпросмотр устарел или изменён.") from None
    expected = dict(actor=str(user.pk), team=str(team_id), project=str(project_id))
    if (
        type(payload) is not dict
        or any(payload.get(k) != v for k, v in expected.items())
        or payload.get("name") != name
    ):
        raise DeletionConflict("Предпросмотр не соответствует запросу.")
    if confirmed is not True:
        raise ValidationError("Подтвердите необратимый запрос удаления.")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    existing = ProjectDeletion.objects.filter(
        team_uuid=team_id, actor_uuid=user.pk, submission_key=key
    ).first()
    if existing:
        if existing.payload_sha256 != digest:
            raise DeletionConflict("Ключ уже использован для другого запроса.")
        return existing
    project = (
        Project.all_objects.select_for_update()
        .filter(pk=project_id, team_id=team_id)
        .first()
    )
    if project is None or project.deletion_requested_at is not None:
        raise DeletionConflict("Удаление проекта уже запрошено.")
    if payload != _payload(user, team_id, project):
        raise DeletionConflict("Проект или объём изменился. Повторите предпросмотр.")
    now = timezone.now()
    with _domain_write():
        receipt = ProjectDeletion.objects.create(
            project_uuid=project.pk,
            team_uuid=team_id,
            actor_uuid=user.pk,
            submission_key=key,
            payload_sha256=digest,
            requested_at=now,
        )
    # Narrow internal writer, not a general-purpose ORM bypass.
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "UPDATE mainapp_project SET deletion_requested_at=%s WHERE id=%s AND team_id=%s AND deletion_requested_at IS NULL",
            [now, project.pk, team_id],
        )
        if cursor.rowcount != 1:
            raise DeletionConflict("Состояние проекта изменилось.")
    return receipt


def deletion_status(user, team_id, receipt_id):
    _owner(user, team_id)
    receipt = ProjectDeletion.objects.filter(pk=receipt_id, team_uuid=team_id).first()
    if receipt is None:
        raise Http404("Операция не найдена.")
    return receipt


# Fixed graph paths. Parameters are always project UUID AND team UUID.
PROJECT_SQL = "SELECT id FROM mainapp_project WHERE id=%s AND team_id=%s"
VERSION_SQL = (
    "SELECT id FROM mainapp_scopeversion WHERE project_id IN (" + PROJECT_SQL + ")"
)
REQUEST_SQL = (
    "SELECT id FROM mainapp_clientrequest WHERE scope_version_id IN ("
    + VERSION_SQL
    + ")"
)
RUN_SQL = (
    "SELECT id FROM mainapp_analysisrun WHERE client_request_id IN ("
    + REQUEST_SQL
    + ")"
)
ATTEMPT_SQL = "SELECT id FROM mainapp_analysisattempt WHERE run_id IN (" + RUN_SQL + ")"
CARD_SQL = (
    "SELECT id FROM mainapp_changecard WHERE client_request_id IN (" + REQUEST_SQL + ")"
)


def _purge_graph(connection, project_id, team_id, now):
    """Internal: caller has authorized intent + wallet/team/project locks or quarantine.

    SQL bypass is deliberately confined to this fixed project graph. Also used on
    an isolated restore authorized by authenticated independent inventory.
    """
    args = [project_id, team_id]
    with connection.cursor() as cursor:
        for kind, query, timestamp in (
            ("run", RUN_SQL, "created_at"),
            ("attempt", ATTEMPT_SQL, "started_at"),
        ):
            table = (
                "mainapp_analysisrun" if kind == "run" else "mainapp_analysisattempt"
            )
            cursor.execute(
                f"SELECT id, {timestamp} FROM {table} WHERE id IN ({query}) AND {timestamp}>%s",
                [*args, now - timedelta(hours=48)],
            )
            for identifier, occurred in cursor.fetchall():
                cursor.execute(
                    "INSERT INTO mainapp_deletedanalysisusage (id,created_at,updated_at,kind,original_uuid,team_uuid,occurred_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (kind,original_uuid) DO NOTHING",
                    [
                        uuid4(),
                        now,
                        now,
                        kind,
                        identifier,
                        team_id,
                        occurred,
                        occurred + timedelta(hours=48),
                    ],
                )
        # Proven zero only before dispatch. Known settled records are untouched.
        cursor.execute(
            f"UPDATE mainapp_analysischarge SET state='settled',charged_microrub=0,settled_at=%s WHERE attempt_id IN ({ATTEMPT_SQL}) AND state!='settled' AND generation_dispatched_at IS NULL",
            [now, *args],
        )
        cursor.execute(
            f"UPDATE mainapp_analysischarge SET state='uncertain' WHERE attempt_id IN ({ATTEMPT_SQL}) AND state!='settled' AND generation_dispatched_at IS NOT NULL",
            args,
        )
        cursor.execute(
            f"UPDATE mainapp_analysischarge SET original_attempt_uuid=attempt_id,attempt_id=NULL WHERE attempt_id IN ({ATTEMPT_SQL})",
            args,
        )
        for table, predicate in (
            ("mainapp_changeevent", "card_id IN (" + CARD_SQL + ")"),
            ("mainapp_changerevision", "card_id IN (" + CARD_SQL + ")"),
            ("mainapp_changecard", "id IN (" + CARD_SQL + ")"),
            ("mainapp_analysisattempt", "id IN (" + ATTEMPT_SQL + ")"),
            ("mainapp_analysisrun", "id IN (" + RUN_SQL + ")"),
            ("mainapp_clientrequest", "id IN (" + REQUEST_SQL + ")"),
            ("mainapp_scopeversion", "id IN (" + VERSION_SQL + ")"),
            ("mainapp_project", "id=%s AND team_id=%s"),
        ):
            cursor.execute(f"DELETE FROM {table} WHERE {predicate}", args)


def maintenance_guard(*, maintenance, processes_stopped, quiescent_since):
    if not maintenance or not processes_stopped or quiescent_since is None:
        raise StorageFailure("maintenance_attestation_required")
    age = (timezone.now() - quiescent_since).total_seconds()
    if age < 90 or age > 86400:
        raise StorageFailure("quiescence_required")


def purge_project(
    receipt_id,
    *,
    execute=False,
    maintenance=False,
    processes_stopped=False,
    quiescent_since=None,
):
    require_live()
    try:
        identifier = UUID(str(receipt_id))
    except (ValueError, TypeError):
        raise StorageFailure("invalid_identifier") from None
    hint = ProjectDeletion.objects.filter(pk=identifier).first()
    if hint is None:
        raise StorageFailure("deletion_receipt_missing")
    if not execute or hint.status == "completed":
        return {"receipt_id": str(hint.pk), "status": hint.status, "execute": False}
    maintenance_guard(
        maintenance=maintenance,
        processes_stopped=processes_stopped,
        quiescent_since=quiescent_since,
    )
    storage = OperatorStorage()
    try:
        with storage.locked(), transaction.atomic():
            money.lock_budget()
            state = installation(lock=True)
            Team.objects.select_for_update().get(pk=hint.team_uuid)
            project = (
                Project.all_objects.select_for_update()
                .filter(pk=hint.project_uuid, team_id=hint.team_uuid)
                .first()
            )
            receipt = ProjectDeletion.objects.select_for_update().get(pk=identifier)
            if receipt.status == "completed":
                return {
                    "receipt_id": str(receipt.pk),
                    "status": "completed",
                    "execute": False,
                }
            if project is None or project.deletion_requested_at is None:
                raise StorageFailure("deletion_marker_missing")
            inventory = storage.initial_inventory(
                state, has_receipts=ProjectDeletion.objects.exists()
            )
            # External durable intent survives DB rollback intentionally.
            inventory = storage.record_deletion(inventory, receipt)
            now = timezone.now()
            _purge_graph(connections["default"], project.pk, project.team_id, now)
            with _domain_write():
                state.inventory_sequence = inventory["sequence"]
                state.save()
                receipt.started_at = receipt.started_at or now
                receipt.completed_at = now
                receipt.status = "completed"
                receipt.error_code = ""
                receipt.save()
            return {
                "receipt_id": str(receipt.pk),
                "status": "completed",
                "execute": True,
            }
    finally:
        storage.close()
