from datetime import timedelta
from unittest.mock import patch
import os
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from mainapp.middleware_recovery import is_quarantined
from mainapp.models.analysis import AnalysisRun
from mainapp.models.analysis_budget import ProviderBudget
from mainapp.models.scope_control import Project
from mainapp.services.analysis import claim_run, reconcile_analysis
from mainapp.services.analysis_backends import get_backend
from mainapp.services.backup_storage import OperatorStorage, StorageFailure
from mainapp.services.data_lifecycle import installation
from mainapp.services.database_backup import (
    _quarantine,
    _target_alias,
    prune_backups,
    restore_backup,
)
from mainapp.tests.test_scope_data_lifecycle import LifecycleStorageMixin
from mainapp.tests.test_scope_analysis import AnalysisBase


@override_settings(SCOPE_ANALYSIS_BACKEND="fixture")
class RecoveryTests(LifecycleStorageMixin, AnalysisBase):
    def quarantine(self, alias, meta, inventory):
        with patch.dict(
            connection.settings_dict, {"NAME": "scope_restore_" + uuid4().hex}
        ):
            _quarantine(alias, meta, inventory)

    def test_internal_restore_helper_refuses_live_database(self):
        with self.assertRaisesMessage(StorageFailure, "isolated_target_required"):
            _quarantine("default", {}, {})

    def test_quarantine_scrubs_external_tombstone_without_inventing_owner_receipt(self):
        run = self.start()
        claim_run(run.pk)
        state = installation()
        storage = OperatorStorage()
        try:
            inventory = storage.load_inventory()
            inventory = storage.save_inventory(
                {
                    **inventory,
                    "sequence": 1,
                    "entries": [
                        dict(
                            project_uuid=str(self.project.pk),
                            team_uuid=str(self.team.pk),
                            receipt_uuid=str(uuid4()),
                            deleted_at=int(timezone.now().timestamp()),
                        )
                    ],
                }
            )
        finally:
            storage.close()
        self.quarantine(
            "default", {"install_id": str(state.install_id), "sequence": 0}, inventory
        )
        self.assertFalse(Project.all_objects.filter(pk=self.project.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.other_project.pk).exists())
        self.assertTrue(ProviderBudget.objects.get().suspended)
        self.assertTrue(is_quarantined())
        self.assertIsNone(claim_run(run.pk))
        with self.assertRaises(ValidationError):
            reconcile_analysis()
        with self.assertRaises(ValidationError):
            get_backend()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/").status_code, 503)
        self.assertEqual(self.client.get(self.url("project_detail")).status_code, 503)

    def test_pending_snapshot_marker_scrubbed_without_inventory_entry(self):
        receipt = self.delete()
        state = installation()
        storage = OperatorStorage()
        try:
            inventory = storage.load_inventory()
        finally:
            storage.close()
        self.quarantine(
            "default", {"install_id": str(state.install_id), "sequence": 0}, inventory
        )
        self.assertFalse(Project.all_objects.filter(pk=self.project.pk).exists())
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, "completed")

    def test_restore_name_fences_even_missing_marker(self):
        with patch.dict(
            connection.settings_dict, {"NAME": "scope_restore_" + uuid4().hex}
        ):
            self.assertTrue(is_quarantined())
            with self.assertRaises(ValidationError):
                get_backend()

    def test_target_requires_exact_separate_database_and_no_url_options(self):
        for url in (
            "",
            "postgres://x:y@localhost/scope_control",
            "postgres://x:y@localhost/scope_restore_bad",
            "postgres://x:y@localhost/scope_restore_"
            + uuid4().hex
            + "?sslmode=disable",
        ):
            with patch.dict(os.environ, {"SCOPE_RESTORE_DATABASE_URL": url}):
                with self.assertRaises(StorageFailure):
                    _target_alias()

    def test_restore_requires_current_authenticated_inventory_before_connect(self):
        storage = OperatorStorage()
        try:
            meta = storage.write_backup(
                installation().install_id,
                0,
                b"PGDMP synthetic",
                pg_version=17,
                server_version=17,
            )
            storage.inventory_dir.unlink(
                storage.inventory_name,
                storage.inventory_dir.read(storage.inventory_name, 100000)[1],
            )
        finally:
            storage.close()
        with patch("mainapp.services.database_backup._target_alias") as connect:
            with self.assertRaisesMessage(StorageFailure, "artifact_missing"):
                restore_backup(meta["backup_id"], execute=True)
            connect.assert_not_called()

    def test_retention_inventory_waits_for_old_physical_backup_and_usage_original_expiry(
        self,
    ):
        run = self.start()
        claim_run(run.pk)
        storage = OperatorStorage()
        try:
            meta = storage.write_backup(
                installation().install_id,
                0,
                b"PGDMP synthetic",
                pg_version=17,
                server_version=17,
            )
        finally:
            storage.close()
        self.purge(self.delete())
        future = timezone.now() + timedelta(days=31)
        with patch(
            "mainapp.services.database_backup.now_seconds",
            return_value=int(future.timestamp()),
        ), patch(
            "mainapp.services.backup_storage.now_seconds",
            return_value=int(future.timestamp()),
        ), patch(
            "mainapp.services.database_backup.timezone.now", return_value=future
        ):
            report = prune_backups()
            self.assertEqual(report["inventory_prunable"], 0)
            self.assertEqual(report["usage_prunable"], 2)
            report = prune_backups(execute=True)
            self.assertEqual(report["inventory_prunable"], 1)
        storage = OperatorStorage()
        try:
            with patch(
                "mainapp.services.backup_storage.now_seconds",
                return_value=int(future.timestamp()),
            ):
                self.assertEqual(storage.load_inventory()["entries"], [])
        finally:
            storage.close()

    def test_restore_nonempty_target_refuses_before_any_pg_process(self):
        from mainapp.services.database_backup import restore_backup
        from unittest.mock import MagicMock

        storage = OperatorStorage()
        try:
            meta = storage.write_backup(
                installation().install_id,
                0,
                b"PGDMP synthetic",
                pg_version=17,
                server_version=17,
            )
        finally:
            storage.close()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [("170007",), (1,)]
        fake = MagicMock()
        fake.cursor.return_value.__enter__.return_value = cursor
        with patch(
            "mainapp.services.database_backup._target_alias",
            return_value="scope_recovery",
        ), patch(
            "mainapp.services.database_backup._bounded_connection", return_value=fake
        ), patch(
            "mainapp.services.database_backup.pg_command"
        ) as command:
            with self.assertRaisesMessage(StorageFailure, "restore_target_not_empty"):
                restore_backup(meta["backup_id"], execute=True)
            command.assert_not_called()

    def test_check_age_fails_when_no_backup_without_recreating_inventory(self):
        with self.assertRaisesMessage(StorageFailure, "backup_or_cleanup_overdue"):
            prune_backups(check_age=True)
