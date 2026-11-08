from pathlib import Path
import os
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from django.test import SimpleTestCase, override_settings
from mainapp.services.backup_storage import (
    OperatorStorage,
    StorageFailure,
    OwnedDirectory,
    RETENTION,
    DUMP_LIMIT,
)
from mainapp.services.database_backup import run_process, pg_command, _environment


class StorageTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="scope-backup-test-", dir="/private/tmp"
        )
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.backups = self.base / "backup"
        self.backups.mkdir(mode=0o700)
        self.inventory = self.base / "external"
        self.inventory.mkdir(mode=0o700)
        config = override_settings(
            SCOPE_BACKUP_ROOT=str(self.backups),
            SCOPE_DELETION_INVENTORY_PATH=str(self.inventory / "deletions.fernet"),
        )
        config.enable()
        self.addCleanup(config.disable)
        self.key = Fernet.generate_key().decode()
        env = patch.dict(os.environ, {"SCOPE_BACKUP_KEY": self.key})
        env.start()
        self.addCleanup(env.stop)
        self.storage = OperatorStorage()
        self.addCleanup(self.storage.close)
        self.install = uuid4()

    def backup(self):
        return self.storage.write_backup(
            self.install,
            0,
            b"PGDMP synthetic private marker",
            pg_version=17,
            server_version=17,
        )

    def test_authenticated_roundtrip_and_no_plaintext_or_key_on_disk(self):
        meta = self.backup()
        actual, dump, info = self.storage.read_backup(meta["backup_id"])
        self.assertEqual(meta, actual)
        self.assertEqual(dump, b"PGDMP synthetic private marker")
        self.assertEqual(info.st_mode & 0o777, 0o600)
        for file in self.backups.iterdir():
            if file.is_file():
                data = file.read_bytes()
                self.assertNotIn(b"private marker", data)
                self.assertNotIn(self.key.encode(), data)
        self.assertEqual(list((self.backups / ".pending").iterdir()), [])

    def test_wrong_key_corruption_size_and_unknown_version_refuse(self):
        meta = self.backup()
        with patch.dict(
            os.environ, {"SCOPE_BACKUP_KEY": Fernet.generate_key().decode()}
        ):
            other = OperatorStorage()
            try:
                with self.assertRaisesMessage(StorageFailure, "authentication"):
                    other.read_backup(meta["backup_id"])
            finally:
                other.close()
        file = self.backups / ("scope-" + meta["backup_id"] + ".fernet")
        file.write_bytes(b"corrupt")
        with self.assertRaises(StorageFailure):
            self.storage.read_backup(meta["backup_id"])
        with patch("mainapp.services.backup_storage.DUMP_LIMIT", 2):
            with self.assertRaisesMessage(StorageFailure, "dump_too_large"):
                self.backup()
        with self.assertRaises(StorageFailure):
            self.storage.read_backup("../escape")

    def test_expiry_prune_dry_run_and_unknown_files_preserved(self):
        meta = self.backup()
        unknown = self.backups / "unknown"
        unknown.write_bytes(b"keep")
        unknown.chmod(0o600)
        with patch(
            "mainapp.services.backup_storage.now_seconds",
            return_value=meta["expires_at"],
        ):
            with self.assertRaisesMessage(StorageFailure, "backup_expired"):
                self.storage.read_backup(meta["backup_id"])
            report = self.storage.prune()
            self.assertEqual(report["expired"], [meta["backup_id"]])
            self.assertTrue(
                (self.backups / ("scope-" + meta["backup_id"] + ".fernet")).exists()
            )
            self.storage.prune(execute=True)
        self.assertTrue(unknown.exists())
        self.assertEqual(list(self.backups.glob("scope-*.fernet")), [])

    def test_missing_key_storage_permissions_symlink_hardlink_and_traversal(self):
        with patch.dict(os.environ, {"SCOPE_BACKUP_KEY": ""}):
            with self.assertRaises(StorageFailure):
                OperatorStorage()
        self.backups.chmod(0o755)
        try:
            with self.assertRaises(StorageFailure):
                OwnedDirectory(self.backups)
        finally:
            self.backups.chmod(0o700)
        link = self.base / "link"
        link.symlink_to(self.backups, target_is_directory=True)
        with self.assertRaises(StorageFailure):
            OwnedDirectory(link)
        with self.assertRaises(StorageFailure):
            OwnedDirectory(str(self.backups) + "/../external")
        original = self.backups / "owned"
        original.write_bytes(b"x")
        original.chmod(0o600)
        os.link(original, self.backups / "hardlink")
        with self.assertRaises(StorageFailure):
            self.storage.backups.read("owned", 100)
        symlink = self.backups / "symlink"
        symlink.symlink_to(original)
        with self.assertRaises(StorageFailure):
            self.storage.backups.read("symlink", 100)

    def test_independent_inventory_namespace_sequence_freshness_and_no_reset(self):
        from types import SimpleNamespace

        state = SimpleNamespace(install_id=self.install, inventory_sequence=0)
        with self.assertRaises(StorageFailure):
            self.storage.initial_inventory(state, has_receipts=True)
        inventory = self.storage.initial_inventory(state, has_receipts=False)
        receipt = SimpleNamespace(project_uuid=uuid4(), team_uuid=uuid4(), pk=uuid4())
        inventory = self.storage.record_deletion(inventory, receipt)
        self.assertEqual(inventory["sequence"], 1)
        self.assertEqual(
            self.storage.record_deletion(inventory, receipt)["sequence"], 1
        )
        with self.assertRaises(StorageFailure):
            self.storage.load_inventory(uuid4())
        with self.assertRaises(StorageFailure):
            self.storage.load_inventory(self.install, minimum_sequence=2)
        with patch(
            "mainapp.services.backup_storage.now_seconds",
            return_value=inventory["refreshed_at"] + 86401,
        ):
            with self.assertRaisesMessage(StorageFailure, "inventory_stale"):
                self.storage.load_inventory(fresh=True)
        (self.inventory / "deletions.fernet").unlink()
        state.inventory_sequence = 1
        with self.assertRaises(StorageFailure):
            self.storage.initial_inventory(state, has_receipts=False)

    def test_atomic_failure_removes_only_own_pending_and_reservation(self):
        with patch(
            "mainapp.services.backup_storage.os.replace",
            side_effect=OSError("private marker"),
        ):
            with self.assertRaisesMessage(StorageFailure, "storage_write_failed"):
                self.backup()
        self.assertEqual(list(self.backups.glob("scope-*.fernet")), [])
        self.assertEqual(list((self.backups / ".pending").iterdir()), [])
        self.storage.backups.atomic("existing", b"original")
        with self.assertRaises(StorageFailure):
            self.storage.backups.atomic("existing", b"replacement")
        self.assertEqual((self.backups / "existing").read_bytes(), b"original")

    def test_pending_prune_age_and_owned_only(self):
        pending = self.storage.backups.pending()
        pending.close()
        file = self.backups / ".pending" / (".part-" + uuid4().hex)
        file.write_bytes(b"ciphertext")
        file.chmod(0o600)
        self.assertEqual(self.storage.prune()["pending"], [])
        os.utime(file, (time.time() - 86401, time.time() - 86401))
        self.assertEqual(len(self.storage.prune()["pending"]), 1)
        self.assertTrue(file.exists())
        self.storage.prune(execute=True)
        self.assertFalse(file.exists())


class ProcessTests(SimpleTestCase):
    def test_concurrent_pipes_no_deadlock_and_input_roundtrip(self):
        script = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
        data = b"x" * 1000000
        self.assertEqual(
            run_process(
                [sys.executable, "-c", script],
                {"PATH": "/usr/bin:/bin"},
                input_data=data,
                output_limit=len(data),
            ),
            data,
        )

    def test_time_size_and_stderr_bounds_safe_error(self):
        for script, kwargs, code in [
            ("import time; time.sleep(3)", {"deadline": 0.1}, "pg_process_timeout"),
            (
                "import sys; sys.stdout.write('x'*10000)",
                {"output_limit": 10},
                "pg_output_too_large",
            ),
            (
                "import sys; sys.stderr.write('private marker')",
                {},
                "pg_unverified_warning",
            ),
            ("import sys; sys.stderr.write('x'*70000)", {}, "pg_stderr_too_large"),
            ("raise SystemExit(2)", {}, "pg_process_failed"),
        ]:
            with self.subTest(code=code), self.assertRaisesMessage(
                StorageFailure, code
            ):
                run_process([sys.executable, "-c", script], {}, **kwargs)

    def test_child_environment_allowlist_and_remote_refusal(self):
        with patch.dict(
            os.environ,
            {
                "PGSERVICE": "private",
                "YANDEX_API_KEY": "private",
                "SCOPE_BACKUP_KEY": "private",
            },
        ):
            env = _environment(
                dict(
                    HOST="127.0.0.1",
                    NAME="synthetic",
                    USER="synthetic",
                    PASSWORD="child-only",
                )
            )
        self.assertEqual(env["PGPASSFILE"], "/dev/null")
        self.assertEqual(env["PGSERVICEFILE"], "/dev/null")
        self.assertNotIn("PGSERVICE", env)
        self.assertNotIn("YANDEX_API_KEY", env)
        self.assertNotIn("SCOPE_BACKUP_KEY", env)
        with self.assertRaises(StorageFailure):
            _environment(dict(HOST="example.invalid"))

    @override_settings(SCOPE_PG_CLIENT_MODE="native", SCOPE_PG_DUMP_BINARY="pg_dump")
    def test_wrong_pg_major_refused(self):
        with patch(
            "mainapp.services.database_backup.run_process",
            return_value=b"pg_dump (PostgreSQL) 18.3\n",
        ):
            with self.assertRaisesMessage(StorageFailure, "pg17_tools_required"):
                pg_command("pg_dump", dict(HOST="127.0.0.1", NAME="synthetic"))

    def test_pg_argv_never_contains_password_and_errors_do_not_log_private_stderr(self):
        from io import StringIO
        import logging
        from mainapp.services import database_backup

        output = StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("mainapp")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        config = {
            "HOST": "127.0.0.1",
            "NAME": "synthetic",
            "USER": "synthetic",
            "PASSWORD": "private-password-marker",
        }
        with override_settings(
            SCOPE_PG_CLIENT_MODE="native", SCOPE_PG_DUMP_BINARY="pg_dump"
        ), patch.object(
            database_backup, "run_process", return_value=b"pg_dump (PostgreSQL) 17.7\n"
        ) as process:
            argv, env = pg_command("pg_dump", config)
        self.assertNotIn(config["PASSWORD"], " ".join(argv))
        self.assertEqual(env["PGPASSWORD"], config["PASSWORD"])
        with self.assertRaises(StorageFailure) as raised:
            run_process(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('private-document-marker')",
                ],
                {},
            )
        self.assertNotIn("private-document-marker", str(raised.exception))
        self.assertNotIn("private-document-marker", output.getvalue())
