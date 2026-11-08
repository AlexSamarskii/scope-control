"""Bounded PG17 operator processes; credentials and plaintext never enter argv/files."""

from copy import deepcopy
from datetime import timedelta
import os
import re
import selectors
import subprocess
import time
from urllib.parse import urlparse, unquote
from uuid import UUID, uuid4

from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone

from mainapp.middleware_recovery import require_live, is_quarantined
from mainapp.models.analysis import _domain_write
from mainapp.models.data_lifecycle import ProjectDeletion
from mainapp.services import analysis_budget as money
from mainapp.services.backup_storage import (
    OperatorStorage,
    StorageFailure,
    DUMP_LIMIT,
    LEAF,
    TOMBSTONE_RETENTION,
    now_seconds,
)
from mainapp.services.data_lifecycle import installation, _purge_graph

PROCESS_SECONDS = 600
STDERR_LIMIT = 65536


def run_process(
    argv, env, *, input_data=None, output_limit=DUMP_LIMIT, deadline=PROCESS_SECONDS
):
    """Drain both outputs while feeding stdin; kill/reap on every bound/failure."""
    child = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    errors = 0
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        for stream, label in ((child.stdout, "out"), (child.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)
        view = memoryview(input_data) if input_data is not None else None
        if view is not None:
            os.set_blocking(child.stdin.fileno(), False)
            if len(view):
                selector.register(child.stdin, selectors.EVENT_WRITE, "in")
            else:
                child.stdin.close()
        end = time.monotonic() + deadline
        while selector.get_map():
            if time.monotonic() >= end:
                raise StorageFailure("pg_process_timeout")
            for key, _ in selector.select(min(0.1, max(0, end - time.monotonic()))):
                if key.data == "in":
                    try:
                        written = os.write(key.fd, view[:65536])
                    except BrokenPipeError:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    view = view[written:]
                    if not view:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if key.data == "out":
                    output.extend(chunk)
                    if len(output) > output_limit:
                        raise StorageFailure("pg_output_too_large")
                else:
                    errors += len(chunk)
                    if errors > STDERR_LIMIT:
                        raise StorageFailure("pg_stderr_too_large")
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise StorageFailure("pg_process_timeout")
        try:
            code = child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise StorageFailure("pg_process_timeout") from None
        if code or errors:
            raise StorageFailure(
                "pg_process_failed" if code else "pg_unverified_warning"
            )
        return bytes(output)
    except (OSError, ValueError):
        raise StorageFailure("pg_process_unavailable") from None
    finally:
        selector.close()
        if child is not None:
            if child.poll() is None:
                # The new process group owns any child it spawned.
                import signal

                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def _environment(config):
    host = str(config.get("HOST") or "")
    if host not in ("", "127.0.0.1", "localhost", "::1"):
        raise StorageFailure("remote_profile_not_accepted")
    # Do not inherit PG service/cert/key/passfile or application provider secrets.
    return {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "PGHOST": host,
        "PGPORT": str(config.get("PORT") or 5432),
        "PGDATABASE": str(config["NAME"]),
        "PGUSER": str(config.get("USER") or ""),
        "PGPASSWORD": str(config.get("PASSWORD") or ""),
        "PGPASSFILE": "/dev/null",
        "PGSERVICEFILE": "/dev/null",
        "PGSYSCONFDIR": "/dev/null",
        "PGCONNECT_TIMEOUT": "5",
        "PGSSLMODE": "disable",
        "PGOPTIONS": "-c statement_timeout=540000 -c lock_timeout=5000",
    }


def pg_command(tool, config):
    if tool not in ("pg_dump", "pg_restore"):
        raise StorageFailure("invalid_pg_tool")
    env = _environment(config)
    mode = settings.SCOPE_PG_CLIENT_MODE
    if mode == "native":
        binary = (
            settings.SCOPE_PG_DUMP_BINARY
            if tool == "pg_dump"
            else settings.SCOPE_PG_RESTORE_BINARY
        )
        argv = [binary]
    elif mode == "container_synthetic":
        if (
            settings.SCOPE_PG_CONTAINER != "scope-control-s1-postgres-1"
            or config["USER"] != "scope_control"
            or not re.fullmatch(
                r"(?:test_)?scope_control(?:_s3b_[a-z0-9_]+)?|scope_restore_[0-9a-f]{32}",
                str(config["NAME"]),
            )
        ):
            raise StorageFailure("synthetic_container_profile_required")
        # Empty, dedicated config directory, explicitly provisioned by operator.
        from mainapp.services.backup_storage import OwnedDirectory

        directory = OwnedDirectory(os.environ.get("SCOPE_DOCKER_CONFIG", ""))
        try:
            if directory.names():
                raise StorageFailure("docker_config_must_be_empty")
        finally:
            directory.close()
        socket = os.environ.get("SCOPE_DOCKER_HOST", "")
        if not re.fullmatch(
            r"unix:///Users/[A-Za-z0-9_-]+/\.colima/default/docker\.sock", socket
        ):
            raise StorageFailure("synthetic_socket_required")
        docker = [
            "docker",
            "--config",
            os.environ["SCOPE_DOCKER_CONFIG"],
            "--host",
            socket,
            "exec",
            "-i",
        ]
        # PG variables are passed by name; passwords never occur in arguments.
        env["PGHOST"] = "/var/run/postgresql"
        env["PGPORT"] = "5432"
        argv = (
            docker
            + [
                item
                for name in sorted(k for k in env if k.startswith("PG"))
                for item in ("--env", name)
            ]
            + [settings.SCOPE_PG_CONTAINER, tool]
        )
    else:
        raise StorageFailure("invalid_pg_profile")
    version = run_process(argv + ["--version"], env, output_limit=1024, deadline=10)
    if not re.fullmatch(
        rb"pg_(?:dump|restore) \(PostgreSQL\) 17\.\d+[^\r\n]*\r?\n?", version
    ):
        raise StorageFailure("pg17_tools_required")
    return argv, env


def _bounded_connection(alias="default"):
    connection = connections[alias]
    connection.settings_dict.setdefault("OPTIONS", {}).setdefault("connect_timeout", 5)
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout=10000")
        cursor.execute("SET lock_timeout=5000")
    return connection


def _server(alias="default"):
    connection = connections[alias]
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        if int(cursor.fetchone()[0]) // 10000 != 17:
            raise StorageFailure("pg17_server_required")
        cursor.execute(
            "SELECT 1 FROM django_migrations WHERE app='mainapp' AND name=%s", [LEAF]
        )
        if cursor.fetchone() is None:
            raise StorageFailure("migration_leaf_mismatch")


def create_backup():
    _bounded_connection()
    require_live()
    storage = OperatorStorage()
    try:
        with storage.locked(), transaction.atomic():
            money.lock_budget()
            from mainapp.models.data_lifecycle import DataLifecycleState

            if not DataLifecycleState.objects.filter(key="installation").exists():
                raise StorageFailure("installation_marker_missing")
            state = installation(lock=True)
            _server()
            inventory = storage.initial_inventory(
                state, has_receipts=ProjectDeletion.objects.exists()
            )
            inventory = storage.save_inventory(inventory)
            if inventory["sequence"] > state.inventory_sequence:
                raise StorageFailure("inventory_pending_purge")
            argv, env = pg_command("pg_dump", connections["default"].settings_dict)
            dump = run_process(
                argv
                + [
                    "--format=custom",
                    "--compress=6",
                    "--no-owner",
                    "--no-acl",
                    "--lock-wait-timeout=5s",
                ],
                env,
            )
            if not dump.startswith(b"PGDMP"):
                raise StorageFailure("invalid_pg_archive")
            return storage.write_backup(
                state.install_id,
                state.inventory_sequence,
                dump,
                pg_version=17,
                server_version=17,
            )
    finally:
        storage.close()


def _target_alias():
    raw = os.environ.get("SCOPE_RESTORE_DATABASE_URL", "")
    try:
        parsed = urlparse(raw)
        name = unquote(parsed.path.lstrip("/"))
        if (
            parsed.scheme not in ("postgres", "postgresql")
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"scope_restore_[0-9a-f]{32}", name)
        ):
            raise ValueError()
        if name == str(connections["default"].settings_dict["NAME"]):
            raise ValueError()
        config = deepcopy(connections["default"].settings_dict)
        config.update(
            NAME=name,
            USER=unquote(parsed.username or ""),
            PASSWORD=unquote(parsed.password or ""),
            HOST=parsed.hostname or "",
            PORT=str(parsed.port or 5432),
            OPTIONS={"connect_timeout": 5},
            ATOMIC_REQUESTS=False,
            CONN_MAX_AGE=0,
        )
        _environment(config)
    except (ValueError, TypeError):
        raise StorageFailure("isolated_target_required") from None
    alias = "scope_recovery"
    if alias in connections:
        connections[alias].close()
    connections.databases[alias] = config
    return alias


def _quarantine(alias, meta, inventory):
    connection = connections[alias]
    if not str(connection.settings_dict["NAME"]).startswith("scope_restore_"):
        raise StorageFailure("isolated_target_required")
    with transaction.atomic(using=alias), connection.cursor() as cursor:
        cursor.execute(
            "SELECT install_id,inventory_sequence FROM mainapp_datalifecyclestate WHERE key='installation' FOR UPDATE"
        )
        state = cursor.fetchone()
        if (
            state is None
            or str(state[0]) != meta["install_id"]
            or state[1] != meta["sequence"]
        ):
            raise StorageFailure("restored_identity_mismatch")
        now = timezone.now()
        cursor.execute(
            "UPDATE mainapp_datalifecyclestate SET quarantined=true,inventory_sequence=%s WHERE key='installation'",
            [inventory["sequence"]],
        )
        cursor.execute("UPDATE mainapp_providerbudget SET suspended=true")
        cursor.execute(
            "SELECT id FROM mainapp_providerbudget WHERE key=%s", [money.BUDGET_KEY]
        )
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO mainapp_providerbudget (id,created_at,updated_at,key,limit_microrub,suspended) VALUES (%s,%s,%s,%s,%s,true)",
                [uuid4(), now, now, money.BUDGET_KEY, money.GLOBAL_LIMIT],
            )
        cursor.execute(
            "UPDATE mainapp_analysischarge SET state='settled',charged_microrub=0,settled_at=%s WHERE state!='settled' AND generation_dispatched_at IS NULL",
            [now],
        )
        cursor.execute(
            "UPDATE mainapp_analysischarge SET state='uncertain' WHERE state!='settled' AND generation_dispatched_at IS NOT NULL"
        )
        cursor.execute(
            "UPDATE mainapp_analysisattempt SET outcome='access_revoked',error_code='access_revoked',finished_at=%s WHERE outcome='running'",
            [now],
        )
        cursor.execute(
            "UPDATE mainapp_analysisrun SET status='failed',error_code='access_revoked',lease_token=NULL,lease_expires_at=NULL,finished_at=%s,validated_result=NULL WHERE status IN ('queued','running')",
            [now],
        )
        targets = {
            (row["project_uuid"], row["team_uuid"]) for row in inventory["entries"]
        }
        cursor.execute(
            "SELECT id,team_id FROM mainapp_project WHERE deletion_requested_at IS NOT NULL"
        )
        targets.update((str(a), str(b)) for a, b in cursor.fetchall())
        for project, team in sorted(targets):
            _purge_graph(connection, UUID(project), UUID(team), now)
            # Only existing copied owner receipts; no invented owner request.
            cursor.execute(
                "UPDATE mainapp_projectdeletion SET status='completed',started_at=COALESCE(started_at,%s),completed_at=%s,error_code='' WHERE project_uuid=%s AND team_uuid=%s AND status='requested'",
                [now, now, project, team],
            )
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def restore_backup(identifier, *, execute=False):
    storage = OperatorStorage()
    try:
        with storage.locked():
            meta, dump, _ = storage.read_backup(identifier)
            inventory = storage.load_inventory(
                meta["install_id"], minimum_sequence=meta["sequence"], fresh=True
            )
            alias = _target_alias()
            connection = _bounded_connection(alias)
            with connection.cursor() as cursor:
                cursor.execute("SHOW server_version_num")
                if int(cursor.fetchone()[0]) // 10000 != 17:
                    raise StorageFailure("pg17_server_required")
                cursor.execute(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'"
                )
                if cursor.fetchone()[0]:
                    raise StorageFailure("restore_target_not_empty")
            argv, env = pg_command("pg_restore", connection.settings_dict)
            if not execute:
                return {
                    "status": "dry_run",
                    "backup_id": meta["backup_id"],
                    "target_quarantined": True,
                }
            # Fresh DB-name guard fences even a restore/transform crash.
            run_process(
                argv
                + [
                    "--dbname",
                    connection.settings_dict["NAME"],
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                ],
                env,
                input_data=dump,
                output_limit=65536,
            )
            _server(alias)
            _quarantine(alias, meta, inventory)
            return verify_recovery(alias=alias, inventory=inventory)
    finally:
        if "scope_recovery" in connections:
            connections["scope_recovery"].close()
        storage.close()


def verify_recovery(*, alias=None, inventory=None):
    alias = alias or _target_alias()
    connection = connections[alias]
    if not is_quarantined(alias):
        raise StorageFailure("quarantine_required")
    _server(alias)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT install_id,inventory_sequence,quarantined FROM mainapp_datalifecyclestate WHERE key='installation'"
        )
        state = cursor.fetchone()
        if not state or not state[2]:
            raise StorageFailure("quarantine_marker_missing")
        if inventory is None:
            storage = OperatorStorage()
            try:
                inventory = storage.load_inventory(
                    state[0], minimum_sequence=state[1], fresh=True
                )
            finally:
                storage.close()
        if inventory["sequence"] != state[1]:
            raise StorageFailure("inventory_sequence_mismatch")
        cursor.execute(
            "SELECT count(*) FROM mainapp_providerbudget WHERE NOT suspended"
        )
        if cursor.fetchone()[0]:
            raise StorageFailure("wallet_not_suspended")
        cursor.execute(
            "SELECT count(*) FROM mainapp_analysisrun WHERE status IN ('queued','running')"
        )
        if cursor.fetchone()[0]:
            raise StorageFailure("restored_jobs_not_fenced")
        cursor.execute(
            "SELECT count(*) FROM mainapp_project WHERE deletion_requested_at IS NOT NULL"
        )
        if cursor.fetchone()[0]:
            raise StorageFailure("pending_project_not_scrubbed")
        for row in inventory["entries"]:
            cursor.execute(
                "SELECT count(*) FROM mainapp_project WHERE id=%s",
                [row["project_uuid"]],
            )
            if cursor.fetchone()[0]:
                raise StorageFailure("deleted_project_resurrected")
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        counts = {}
        for table in (
            "project",
            "scopeversion",
            "clientrequest",
            "analysisrun",
            "changerevision",
            "analysischarge",
        ):
            cursor.execute("SELECT count(*) FROM mainapp_" + table)
            counts[table] = cursor.fetchone()[0]
    return {
        "status": "verified_quarantine",
        "migration_leaf": LEAF,
        "inventory_sequence": state[1],
        "counts": counts,
    }


def prune_backups(*, execute=False, check_age=False):
    _bounded_connection()
    require_live()
    storage = OperatorStorage()
    try:
        with storage.locked(), transaction.atomic():
            money.lock_budget()
            state = installation(lock=True)
            inventory = storage.load_inventory(
                state.install_id, minimum_sequence=state.inventory_sequence
            )
            report = storage.prune(execute=execute)
            # Refresh ONLY the current canonical file, never a DB-derived inventory.
            now = now_seconds()
            safe = True
            oldest = None
            for name in storage.backups.names():
                match = re.fullmatch(r"scope-([0-9a-f-]{36})\.fernet", name)
                if not match:
                    if name != ".pending":
                        safe = False
                    continue
                try:
                    meta, _, _ = storage.read_backup(match[1], allow_expired=True)
                except StorageFailure:
                    safe = False
                    continue
                # Physical remnants retain tombstones, even expired until removed.
                oldest = min(oldest or meta["created_at"], meta["created_at"])
            eligible = [
                row
                for row in inventory["entries"]
                if now - row["deleted_at"] >= TOMBSTONE_RETENTION
                and safe
                and (oldest is None or oldest > row["deleted_at"])
            ]
            report["inventory_prunable"] = len(eligible)
            with connections["default"].cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM mainapp_deletedanalysisusage WHERE expires_at<=%s",
                    [timezone.now()],
                )
                report["usage_prunable"] = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT count(*) FROM mainapp_projectdeletion WHERE status='completed' AND completed_at<=%s",
                    [timezone.now() - timedelta(days=30)],
                )
                report["receipts_old"] = cursor.fetchone()[0]
                if execute:
                    # Remove receipt only when its independent tombstone is safe to expire.
                    for row in eligible:
                        cursor.execute(
                            "DELETE FROM mainapp_projectdeletion WHERE id=%s AND status='completed' AND completed_at<=%s",
                            [row["receipt_uuid"], timezone.now() - timedelta(days=30)],
                        )
                    cursor.execute(
                        "DELETE FROM mainapp_deletedanalysisusage WHERE expires_at<=%s",
                        [timezone.now()],
                    )
            if execute:
                storage.save_inventory(
                    {
                        **inventory,
                        "entries": [
                            row for row in inventory["entries"] if row not in eligible
                        ],
                    }
                )
            latest = report["latest_created_at"]
            report["backup_fresh"] = bool(latest and now - latest <= 86400)
            report["cleanup_due"] = bool(
                report["unknown"]
                or (
                    not execute
                    and (
                        report["expired"]
                        or report["pending"]
                        or eligible
                        or report["usage_prunable"]
                    )
                )
            )
            if check_age and (not report["backup_fresh"] or report["cleanup_due"]):
                raise StorageFailure("backup_or_cleanup_overdue")
            return report
    finally:
        storage.close()
