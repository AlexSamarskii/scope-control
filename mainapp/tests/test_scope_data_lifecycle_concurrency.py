from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.http import Http404
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from mainapp.models import Team, TeamMembership
from mainapp.models.scope_control import Project, ScopeVersion
from mainapp.models.analysis import AnalysisRun, AnalysisAttempt
from mainapp.models.analysis_budget import AnalysisCharge
from mainapp.models.data_lifecycle import ProjectDeletion
from mainapp.services.analysis import (
    create_request,
    start_analysis,
    claim_run,
    mark_generation,
    record_receipt,
)
from mainapp.services.analysis_provider import ProviderReply
from mainapp.services.data_lifecycle import (
    request_deletion,
    preview_deletion,
    DeletionConflict,
)
from mainapp.services.scope_control import create_project, confirm_scope
from mainapp.services.change_exports import current_cards
from mainapp.tests.test_scope_data_lifecycle import LifecycleStorageMixin
from usermodel.models import User


@override_settings(SCOPE_ANALYSIS_BACKEND="fixture")
class LifecycleConcurrencyTests(LifecycleStorageMixin, TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.owner = User.objects.create_user(email="s3b-race@example.invalid")
        self.team = Team.objects.create(name="S3b race", slug="s3b-race")
        TeamMembership.objects.create(team=self.team, user=self.owner, role="owner")
        self.project = create_project(self.owner, self.team.pk, name="S3b synthetic")
        self.version = confirm_scope(
            self.owner, self.team.pk, self.project.pk, body="Scope", confirmed=True
        )
        self.item = create_request(
            self.owner,
            self.team.pk,
            self.project.pk,
            version_id=self.version.pk,
            body="Request",
            submission_key=uuid4(),
        )
        super().setUp()

    def race(self, left, right):
        barrier = Barrier(2)

        def invoke(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    return fn()
                except (Http404, DeletionConflict):
                    return "fenced"
            finally:
                connections["default"].close()

        with patch(
            "mainapp.services.analysis.publish_run", return_value=True
        ), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(invoke, fn) for fn in (left, right)]
            return [f.result(timeout=30) for f in futures]

    def start(self):
        with patch("mainapp.services.analysis.publish_run", return_value=True):
            return start_analysis(
                self.owner,
                self.team.pk,
                self.project.pk,
                self.item.pk,
                start_key=uuid4(),
                external_processing_confirmed=True,
            )

    def test_duplicate_owner_requests_commit_once(self):
        token = preview_deletion(
            self.owner, self.team.pk, self.project.pk, name=self.project.name
        )
        key = uuid4()

        def request():
            return request_deletion(
                self.owner,
                self.team.pk,
                self.project.pk,
                name=self.project.name,
                preview_token=token,
                submission_key=key,
                confirmed=True,
            ).pk

        ids = self.race(request, request)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ProjectDeletion.objects.count(), 1)

    def test_delete_vs_start_is_fenced_after_commit(self):
        self.race(self.delete, self.start)
        with self.assertRaises(Http404):
            self.start()
        for run in AnalysisRun.objects.all():
            self.assertIsNone(claim_run(run.pk))
        self.assertFalse(AnalysisAttempt.objects.exists())

    def test_delete_vs_claim_creates_no_post_marker_attempt(self):
        run = self.start()
        self.race(self.delete, lambda: claim_run(run.pk))
        before = AnalysisAttempt.objects.count()
        self.assertIsNone(claim_run(run.pk))
        self.assertEqual(AnalysisAttempt.objects.count(), before)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    @override_settings(
        SCOPE_ANALYSIS_BACKEND="yandex",
        SCOPE_YANDEX_FOLDER_ID="synthetic-folder",
        SCOPE_YANDEX_PROCESSING_APPROVED=True,
        SCOPE_YANDEX_PRICE_CONFIRMED_ON=timezone.now().date().isoformat(),
    )
    def test_delete_vs_dispatch_never_dispatches_after_marker(self):
        run = self.start()
        claim = claim_run(run.pk)
        self.race(self.delete, lambda: mark_generation(run.pk, claim.token))
        self.assertFalse(mark_generation(run.pk, claim.token))
        self.assertEqual(AnalysisCharge.objects.count(), 1)

    @override_settings(
        SCOPE_ANALYSIS_BACKEND="yandex",
        SCOPE_YANDEX_FOLDER_ID="synthetic-folder",
        SCOPE_YANDEX_PROCESSING_APPROVED=True,
        SCOPE_YANDEX_PRICE_CONFIRMED_ON=timezone.now().date().isoformat(),
    )
    def test_delete_vs_late_receipt_only_changes_finances(self):
        run = self.start()
        claim = claim_run(run.pk)
        mark_generation(run.pk, claim.token)
        reply = ProviderReply(
            None,
            {"inputTextTokens": 200, "completionTokens": 100, "totalTokens": 300},
            "synthetic",
        )
        self.race(self.delete, lambda: record_receipt(run.pk, claim.token, reply))
        run.refresh_from_db()
        self.assertIsNone(run.validated_result)
        self.assertEqual(AnalysisCharge.objects.get().charged_microrub, 240000)
        self.assertIsNone(claim_run(run.pk))

    def test_delete_vs_scope_confirmation_cannot_write_after_marker(self):
        def confirm():
            return confirm_scope(
                self.owner,
                self.team.pk,
                self.project.pk,
                body="v2",
                expected_version=self.version.pk,
                confirmed=True,
            )

        # If confirmation wins, deletion preview becomes stale and must be renewed.
        results = self.race(self.delete, confirm)
        if not ProjectDeletion.objects.exists():
            self.delete()
        with self.assertRaises(Http404):
            confirm()
        self.assertIn(
            ScopeVersion.objects.filter(project_id=self.project.pk).count(), (1, 2)
        )

    def test_delete_vs_export_blocks_new_reads(self):
        self.race(
            self.delete,
            lambda: list(current_cards(self.owner, self.team.pk, self.project.pk)),
        )
        with self.assertRaises(Http404):
            current_cards(self.owner, self.team.pk, self.project.pk)

    def test_duplicate_purge_is_one_completion(self):
        run = self.start()
        claim_run(run.pk)
        receipt = self.delete()
        results = self.race(lambda: self.purge(receipt), lambda: self.purge(receipt))
        self.assertEqual([r["status"] for r in results], ["completed", "completed"])
        self.assertFalse(Project.all_objects.filter(pk=self.project.pk).exists())
        self.assertEqual(ProjectDeletion.objects.count(), 1)

    def test_delete_vs_s3a_apply_serializes_and_invalidates_stale_preview(self):
        from mainapp.tests.test_scope_change_control import ChangeBase
        from mainapp.services.change_control import apply_scope_change

        revision = ChangeBase.decision(self)
        ChangeBase.shared(self, revision)
        event = ChangeBase.response(self, revision)
        data = ChangeBase.apply_data(self, revision, event)
        token = ChangeBase.preview(self, data, revision.card_id)

        def apply():
            return apply_scope_change(
                self.owner,
                self.team.pk,
                self.project.pk,
                revision.card_id,
                **data,
                preview_token=token,
                confirmed=True
            )

        self.race(self.delete, apply)
        if not ProjectDeletion.objects.exists():
            self.delete()
        with self.assertRaises(Http404):
            apply()
        self.assertIn(
            ScopeVersion.objects.filter(project_id=self.project.pk).count(), (1, 2)
        )

    @override_settings(
        SCOPE_ANALYSIS_BACKEND="yandex",
        SCOPE_YANDEX_FOLDER_ID="synthetic-folder",
        SCOPE_YANDEX_PROCESSING_APPROVED=True,
        SCOPE_YANDEX_PRICE_CONFIRMED_ON=timezone.now().date().isoformat(),
    )
    def test_purge_vs_late_receipt_keeps_valid_charge_and_never_raises_missing_run(
        self,
    ):
        run = self.start()
        claim = claim_run(run.pk)
        mark_generation(run.pk, claim.token)
        receipt = self.delete()
        reply = ProviderReply(
            None,
            {"inputTextTokens": 200, "completionTokens": 100, "totalTokens": 300},
            "synthetic",
        )
        self.race(
            lambda: self.purge(receipt),
            lambda: record_receipt(run.pk, claim.token, reply),
        )
        self.assertFalse(Project.all_objects.filter(pk=self.project.pk).exists())
        charge = AnalysisCharge.objects.get()
        self.assertIsNone(charge.attempt_id)
        self.assertIn(charge.state, ("settled", "uncertain"))
        self.assertFalse(record_receipt(run.pk, claim.token, reply))
