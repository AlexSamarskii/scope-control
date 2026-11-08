from datetime import timedelta
from pathlib import Path
import os
import tempfile
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from django.contrib import admin
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from mainapp.models.analysis import AnalysisRun, AnalysisAttempt, _domain_write
from mainapp.models.analysis_budget import AnalysisCharge, ProviderBudget
from mainapp.models.data_lifecycle import ProjectDeletion, DeletedAnalysisUsage
from mainapp.models.scope_control import Project, ScopeVersion
from mainapp.services import analysis_budget as money
from mainapp.services.analysis import (
    claim_run,
    execute_run,
    finish_run,
    create_request,
    reconcile_analysis,
    AnalysisQuota,
)
from mainapp.services.analysis_backends import fixture_response
from mainapp.services.analysis_provider import public_profile, RESERVE_MICRORUB
from mainapp.services.backup_storage import OperatorStorage, StorageFailure
from mainapp.services.data_lifecycle import (
    preview_deletion,
    request_deletion,
    deletion_status,
    purge_project,
    DeletionConflict,
    installation,
)
from mainapp.tests.test_scope_analysis import AnalysisBase


class LifecycleStorageMixin:
    def setUp(self):
        super().setUp()
        self.storage_temp = tempfile.TemporaryDirectory(
            prefix="scope-s3b-", dir="/private/tmp"
        )
        self.addCleanup(self.storage_temp.cleanup)
        base = Path(self.storage_temp.name)
        (base / "backups").mkdir(mode=0o700)
        (base / "inventory").mkdir(mode=0o700)
        self.storage_settings = override_settings(
            SCOPE_BACKUP_ROOT=str(base / "backups"),
            SCOPE_DELETION_INVENTORY_PATH=str(base / "inventory" / "deletions.fernet"),
        )
        self.storage_settings.enable()
        self.addCleanup(self.storage_settings.disable)
        self.key_env = patch.dict(
            os.environ, {"SCOPE_BACKUP_KEY": Fernet.generate_key().decode()}
        )
        self.key_env.start()
        self.addCleanup(self.key_env.stop)
        with __import__("django.db", fromlist=["transaction"]).transaction.atomic():
            money.lock_budget()
            state = installation()
        storage = OperatorStorage()
        try:
            with storage.locked():
                storage.initial_inventory(state, has_receipts=False)
        finally:
            storage.close()

    def delete(self, **kwargs):
        token = preview_deletion(
            self.owner, self.team.pk, self.project.pk, name=self.project.name
        )
        return request_deletion(
            self.owner,
            self.team.pk,
            self.project.pk,
            **dict(
                name=self.project.name,
                preview_token=token,
                submission_key=uuid4(),
                confirmed=True,
                **kwargs,
            ),
        )

    def purge(self, receipt, **kwargs):
        return purge_project(
            receipt.pk,
            **dict(
                execute=True,
                maintenance=True,
                processes_stopped=True,
                quiescent_since=timezone.now() - timedelta(seconds=91),
                **kwargs,
            ),
        )


@override_settings(SCOPE_ANALYSIS_BACKEND="fixture")
class LifecycleTests(LifecycleStorageMixin, AnalysisBase):
    def test_owner_preview_unchecked_and_pending_http(self):
        self.client.force_login(self.owner)
        url = self.url("project_delete")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("checked", str(response.content))
        response = self.client.post(
            url,
            {"name": self.project.name, "submission_key": uuid4(), "action": "preview"},
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertFalse(form.initial["confirmed"])
        data = {**form.initial, "action": "confirm", "confirmed": "on"}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response.url), "ожидает оператора")
        for name, extra in [
            ("project_detail", {}),
            ("scope_version", {"version_id": self.version.pk}),
            ("request_detail", {"request_id": self.item.pk}),
            ("request_create", {}),
            ("change_export", {}),
        ]:
            self.assertEqual(self.client.get(self.url(name, **extra)).status_code, 404)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
        self.assertTrue(Project.all_objects.filter(pk=self.project.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.other_project.pk).exists())

    def test_only_active_owner_can_delete_and_read_status(self):
        token = preview_deletion(
            self.owner, self.team.pk, self.project.pk, name=self.project.name
        )
        for role in ("admin", "member", "viewer"):
            self.membership.role = role
            self.membership.save()
            with self.assertRaises(PermissionDenied):
                preview_deletion(
                    self.owner, self.team.pk, self.project.pk, name=self.project.name
                )
        self.membership.role = "owner"
        self.membership.save()
        receipt = self.delete()
        with self.assertRaises(Http404):
            deletion_status(self.outsider, self.team.pk, receipt.pk)
        self.membership.access_expires_at = timezone.now() - timedelta(seconds=1)
        self.membership.save()
        with self.assertRaises(Http404):
            deletion_status(self.owner, self.team.pk, receipt.pk)

    def test_preview_signature_expiry_stale_scope_and_name(self):
        token = preview_deletion(
            self.owner, self.team.pk, self.project.pk, name=self.project.name
        )
        args = dict(
            name=self.project.name,
            preview_token=token,
            submission_key=uuid4(),
            confirmed=True,
        )
        with self.assertRaises(DeletionConflict):
            request_deletion(self.owner, self.team.pk, self.other_project.pk, **args)
        with self.assertRaises(DeletionConflict):
            request_deletion(
                self.owner,
                self.team.pk,
                self.project.pk,
                **{**args, "preview_token": token + "x"},
            )
        with patch(
            "django.core.signing.time.time",
            return_value=__import__("time").time() + 601,
        ):
            with self.assertRaises(DeletionConflict):
                request_deletion(self.owner, self.team.pk, self.project.pk, **args)
        from mainapp.services.scope_control import confirm_scope

        confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            body="New scope",
            expected_version=self.version.pk,
            confirmed=True,
        )
        with self.assertRaises(DeletionConflict):
            request_deletion(self.owner, self.team.pk, self.project.pk, **args)
        with self.assertRaises(ValidationError):
            preview_deletion(self.owner, self.team.pk, self.project.pk, name="wrong")

    def test_request_replay_and_mismatch(self):
        token = preview_deletion(
            self.owner, self.team.pk, self.project.pk, name=self.project.name
        )
        args = dict(
            name=self.project.name,
            preview_token=token,
            submission_key=uuid4(),
            confirmed=True,
        )
        with self.assertRaises(ValidationError):
            request_deletion(
                self.owner,
                self.team.pk,
                self.project.pk,
                **{**args, "confirmed": False},
            )
        receipt = request_deletion(self.owner, self.team.pk, self.project.pk, **args)
        self.assertEqual(
            request_deletion(self.owner, self.team.pk, self.project.pk, **args).pk,
            receipt.pk,
        )
        with self.assertRaises(DeletionConflict):
            request_deletion(
                self.owner,
                self.team.pk,
                self.project.pk,
                **{**args, "submission_key": uuid4()},
            )
        self.assertEqual(ProjectDeletion.objects.count(), 1)

    def test_marker_and_historical_guards_remain(self):
        self.delete()
        self.project.deletion_requested_at = None
        with self.assertRaises(ValidationError):
            self.project.save()
        with self.assertRaises(ValidationError):
            Project.all_objects.filter(pk=self.project.pk).update(
                deletion_requested_at=None
            )
        with self.assertRaises(ValidationError):
            ScopeVersion.objects.filter(pk=self.version.pk).delete()
        with self.assertRaises(ValidationError):
            Project.all_objects.bulk_update([self.project], ["deletion_requested_at"])
        with self.assertRaises(ValidationError):
            Project.all_objects.bulk_create(
                [
                    Project(
                        team=self.team, name="X", deletion_requested_at=timezone.now()
                    )
                ]
            )

    def test_pending_fences_start_claim_finish_and_admin(self):
        run = self.start()
        claim = claim_run(run.pk)
        self.delete()
        self.assertTrue(
            finish_run(run.pk, claim.token, raw=fixture_response(claim.packet))
        )
        run.refresh_from_db()
        self.assertEqual(run.error_code, "access_revoked")
        self.assertIsNone(run.validated_result)
        with self.assertRaises(Http404):
            self.start()
        with self.assertRaises(Http404):
            create_request(
                self.owner,
                self.team.pk,
                self.project.pk,
                version_id=self.version.pk,
                body="X",
                submission_key=uuid4(),
            )
        self.assertIsNone(claim_run(run.pk))
        reconcile_analysis()
        request = RequestFactory().get("/admin/")
        request.user = self.owner
        request.user.is_superuser = True
        for obj in (self.project, self.version, self.item, run):
            model_admin = admin.site._registry[type(obj)]
            self.assertFalse(model_admin.has_view_permission(request, obj))
            self.assertFalse(
                model_admin.get_queryset(request).filter(pk=obj.pk).exists()
            )

    def test_purge_preserves_other_tenant_and_quota_and_stale_uuid(self):
        run = self.start()
        execute_run(run.pk)
        receipt = self.delete()
        self.assertFalse(purge_project(receipt.pk)["execute"])
        with self.assertRaises(StorageFailure):
            purge_project(receipt.pk, execute=True)
        with self.assertRaises(StorageFailure):
            purge_project(
                receipt.pk,
                execute=True,
                maintenance=True,
                processes_stopped=True,
                quiescent_since=timezone.now(),
            )
        self.assertEqual(self.purge(receipt)["status"], "completed")
        self.assertFalse(Project.all_objects.filter(pk=self.project.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.other_project.pk).exists())
        self.assertEqual(
            DeletedAnalysisUsage.objects.filter(team_uuid=self.team.pk).count(), 2
        )
        self.assertIsNone(claim_run(run.pk))
        execute_run(run.pk)
        self.assertFalse(self.purge(receipt)["execute"])
        storage = OperatorStorage()
        try:
            self.assertEqual(len(storage.load_inventory()["entries"]), 1)
        finally:
            storage.close()

    def test_inventory_failure_and_sql_failure_do_not_partial_purge(self):
        run = self.start()
        execute_run(run.pk)
        from mainapp.tests.test_scope_change_control import ChangeBase

        revision = ChangeBase.decision(self)
        receipt = self.delete()
        with patch.object(
            OperatorStorage,
            "record_deletion",
            side_effect=StorageFailure("storage_write_failed"),
        ):
            with self.assertRaises(StorageFailure):
                self.purge(receipt)
        self.assertTrue(Project.all_objects.filter(pk=self.project.pk).exists())
        from django.db import DatabaseError, connections
        from mainapp.services.data_lifecycle import _purge_graph

        def fail_after_purge(*args):
            _purge_graph(*args)
            # Real PG error after every graph statement; the whole DB transaction
            # must roll back while the external durable inventory remains.
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT 1/0")

        with patch(
            "mainapp.services.data_lifecycle._purge_graph",
            side_effect=fail_after_purge,
        ):
            with self.assertRaises(DatabaseError):
                self.purge(receipt)
        self.assertTrue(Project.all_objects.filter(pk=self.project.pk).exists())
        self.assertTrue(AnalysisRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(type(revision).objects.filter(pk=revision.pk).exists())
        self.assertFalse(DeletedAnalysisUsage.objects.exists())
        storage = OperatorStorage()
        try:
            self.assertEqual(len(storage.load_inventory()["entries"]), 1)
        finally:
            storage.close()
        self.purge(receipt)
        self.assertEqual(ProjectDeletion.objects.get(pk=receipt.pk).status, "completed")

    def test_detached_known_unknown_and_undispatched_money_preserved(self):
        runs = []
        charges = []
        from mainapp.services.analysis_provider import ProviderReply

        for state in ("known", "unknown", "not_sent"):
            item = self.make_request()
            run = self.start(item)
            claim = claim_run(run.pk)
            attempt = AnalysisAttempt.objects.get(run=run)
            with _domain_write():
                charge = AnalysisCharge.objects.create(
                    attempt=attempt,
                    budget=ProviderBudget.objects.get(),
                    profile={"microrub_per_token": 1},
                    reserved_microrub=100,
                )
                if state != "not_sent":
                    charge.generation_dispatched_at = timezone.now()
                    charge.save()
                if state == "known":
                    charge.state = "settled"
                    charge.charged_microrub = 15
                    charge.settled_at = timezone.now()
                    charge.save()
            charges.append(charge)
        before = money.used_amount(AnalysisCharge.objects.all())
        self.purge(self.delete())
        for charge in charges:
            charge.refresh_from_db()
            self.assertIsNone(charge.attempt_id)
            self.assertIsNotNone(charge.original_attempt_uuid)
        self.assertEqual(
            [c.state for c in charges], ["settled", "uncertain", "settled"]
        )
        self.assertEqual([c.charged_microrub for c in charges], [15, None, 0])
        self.assertEqual(money.used_amount(AnalysisCharge.objects.all()), before - 100)
        self.assertEqual(ProviderBudget.objects.get().limit_microrub, 100_000_000)

    def test_purge_archive_keeps_original_time_and_enforces_quota(self):
        run = self.start()
        execute_run(run.pk)
        created = run.created_at
        self.purge(self.delete())
        archived = DeletedAnalysisUsage.objects.get(kind="run")
        self.assertEqual(archived.occurred_at, created)
        self.assertEqual(archived.expires_at, created + timedelta(hours=48))
        from mainapp.services.scope_control import create_project, confirm_scope
        from mainapp.services.analysis import start_analysis

        project = create_project(self.owner, self.team.pk, name="New")
        version = confirm_scope(
            self.owner, self.team.pk, project.pk, body="Scope", confirmed=True
        )
        item = create_request(
            self.owner,
            self.team.pk,
            project.pk,
            version_id=version.pk,
            body="Ask",
            submission_key=uuid4(),
        )
        with override_settings(SCOPE_ANALYSIS_RUNS_PER_HOUR=1):
            with self.assertRaises(AnalysisQuota):
                start_analysis(
                    self.owner, self.team.pk, project.pk, item.pk, start_key=uuid4()
                )

    def test_csrf_owner_get_is_read_only_and_inactive_owner_denied(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.get(self.url("project_delete")).status_code, 200)
        self.assertFalse(ProjectDeletion.objects.exists())
        self.assertEqual(
            client.post(
                self.url("project_delete"),
                {
                    "action": "preview",
                    "name": self.project.name,
                    "submission_key": uuid4(),
                },
            ).status_code,
            403,
        )
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        with self.assertRaises(Http404):
            self.delete()

    def test_same_key_other_project_payload_conflicts_and_rename_stales_preview(self):
        key = uuid4()
        token = preview_deletion(
            self.owner, self.team.pk, self.project.pk, name=self.project.name
        )
        from mainapp.services.scope_control import create_project, rename_project

        project = create_project(self.owner, self.team.pk, name="Second")
        other_token = preview_deletion(
            self.owner, self.team.pk, project.pk, name=project.name
        )
        request_deletion(
            self.owner,
            self.team.pk,
            project.pk,
            name=project.name,
            preview_token=other_token,
            submission_key=key,
            confirmed=True,
        )
        with self.assertRaises(DeletionConflict):
            request_deletion(
                self.owner,
                self.team.pk,
                self.project.pk,
                name=self.project.name,
                preview_token=token,
                submission_key=key,
                confirmed=True,
            )
        rename_project(self.owner, self.team.pk, self.project.pk, name="Renamed")
        with self.assertRaises(DeletionConflict):
            request_deletion(
                self.owner,
                self.team.pk,
                self.project.pk,
                name=self.project.name,
                preview_token=token,
                submission_key=uuid4(),
                confirmed=True,
            )

    def test_pending_change_draft_card_history_and_mutations_closed(self):
        from mainapp.tests.test_scope_change_control import ChangeBase
        from mainapp.services.change_control import get_card

        revision = ChangeBase.decision(self)
        self.client.force_login(self.owner)
        self.delete()
        for name, extra in [
            ("change_detail", {"card_id": revision.card_id}),
            ("change_revise", {"card_id": revision.card_id}),
            ("change_draft", {"card_id": revision.card_id, "revision_id": revision.pk}),
            ("change_event", {"card_id": revision.card_id}),
            ("change_apply", {"card_id": revision.card_id}),
        ]:
            self.assertEqual(self.client.get(self.url(name, **extra)).status_code, 404)
        with self.assertRaises(Http404):
            get_card(self.owner, self.team.pk, self.project.pk, revision.card_id)

    def test_quota_attempt_archive_blocks_new_daily_claim(self):
        run = self.start()
        execute_run(run.pk)
        self.purge(self.delete())
        from mainapp.services.scope_control import create_project, confirm_scope
        from mainapp.services.analysis import start_analysis

        project = create_project(self.owner, self.team.pk, name="Next")
        version = confirm_scope(
            self.owner, self.team.pk, project.pk, body="Scope", confirmed=True
        )
        item = create_request(
            self.owner,
            self.team.pk,
            project.pk,
            version_id=version.pk,
            body="Ask",
            submission_key=uuid4(),
        )
        run = start_analysis(
            self.owner, self.team.pk, project.pk, item.pk, start_key=uuid4()
        )
        with override_settings(SCOPE_ANALYSIS_ATTEMPTS_PER_DAY=1):
            self.assertIsNone(claim_run(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.error_code, "quota_exceeded")
