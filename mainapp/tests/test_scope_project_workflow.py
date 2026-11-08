from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import BadRequest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from mainapp.models.analysis import ClientRequest, AnalysisRun, AnalysisAttempt
from mainapp.models.analysis_budget import AnalysisCharge
from mainapp.models.change_control import ChangeCard, ChangeRevision, ChangeEvent
from mainapp.services.project_workflow import workflow_for, describe_requests
from mainapp.services.scope_control import create_project, confirm_scope
from mainapp.tests.test_scope_change_control import ChangeBase


class WorkflowTests(ChangeBase):
    def flow(self, item=None):
        return workflow_for(
            self.owner, self.team.pk, self.project.pk, (item or self.item).pk
        )

    def test_empty_project_then_scope_leads_to_request(self):
        project = create_project(self.owner, self.team.pk, name="Fresh")
        result = workflow_for(self.owner, self.team.pk, project.pk)
        self.assertEqual(result["state"], "empty")
        self.assertEqual(result["steps"][0]["status"], "Текущий шаг")
        self.assertIn("/confirm/", result["action"]["url"])
        confirm_scope(
            self.owner, self.team.pk, project.pk, body="Scope", confirmed=True
        )
        result = workflow_for(self.owner, self.team.pk, project.pk)
        self.assertEqual(result["state"], "scope")
        self.assertIn("/requests/new/", result["action"]["url"])

    def test_request_without_card_has_manual_decision(self):
        result = self.flow()
        self.assertEqual(result["state"], "decision")
        self.assertTrue(result["manual"])
        self.assertEqual(result["mode_label"], "Ручной режим")
        self.assertEqual(
            result["action"]["url"], self.url("change_create", request_id=self.item.pk)
        )
        self.assertEqual([s["number"] for s in result["steps"] if s["current"]], [3])

    def test_all_three_human_categories_get_different_next_actions(self):
        for category, expected in [
            ("included", "included"),
            ("insufficient_evidence", "unclear"),
            ("possible_change", "draft"),
        ]:
            item = self.make_request()
            data = (
                {}
                if category == "possible_change"
                else dict(amount=None, currency="", deadline_effect="")
            )
            self.decision(item, category=category, **data)
            result = self.flow(item)
            self.assertEqual(result["state"], expected)
            if category == "included":
                self.assertEqual(result["steps"][3]["status"], "Не требуется")
            if category == "insufficient_evidence":
                self.assertIn("/revise/", result["action"]["url"])

    def test_unknown_estimate_differs_from_explicit_zero(self):
        revision = self.decision(amount=None, currency="", deadline_effect="")
        self.assertEqual(self.flow()["state"], "incomplete")
        self.revise(
            revision,
            amount="0.00",
            currency="RUB",
            deadline_effect="Без изменения срока.",
        )
        result = self.flow()
        self.assertEqual(result["state"], "draft")
        self.assertEqual(result["snapshot"]["amount"], "0.00")
        self.assertFalse(result["action"]["requires_write"])

    def test_missing_deadline_keeps_estimate_incomplete(self):
        self.decision(deadline_effect="")
        self.assertEqual(self.flow()["state"], "incomplete")

    def test_shared_then_accepted_does_not_imply_applied(self):
        revision = self.decision()
        self.shared(revision)
        self.assertEqual(self.flow()["state"], "shared")
        self.response(revision)
        result = self.flow()
        self.assertEqual(result["state"], "accepted")
        self.assertIn("/apply/", result["action"]["url"])
        self.assertEqual(self.project.versions.count(), 1)

    def test_applied_uses_resulting_version_even_with_old_source(self):
        revision, event = self.accepted()
        version = self.apply(revision, event)
        result = self.flow()
        self.assertEqual(result["state"], "applied")
        self.assertFalse(result["stale"])
        self.assertEqual(result["resulting_version"], version.number)
        self.item.refresh_from_db()
        self.assertEqual(self.item.scope_version_id, self.version.pk)

    def test_rejected_never_suggests_apply(self):
        revision = self.decision()
        self.shared(revision)
        self.response(revision, kind="rejected")
        result = self.flow()
        self.assertEqual(result["state"], "rejected")
        self.assertIn("/revise/", result["action"]["url"])
        self.assertNotIn("/apply/", result["action"]["url"])

    def test_stale_source_takes_precedence_over_accepted(self):
        self.accepted()
        confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            body="v2",
            expected_version=self.version.pk,
            confirmed=True,
        )
        result = self.flow()
        self.assertEqual(result["state"], "stale")
        self.assertTrue(result["stale"])
        self.assertEqual(result["request"].scope_version_id, self.version.pk)
        self.assertIn("/requests/new/", result["action"]["url"])
        self.assertNotIn("/apply/", result["action"]["url"])

    def test_new_revision_resets_old_acceptance(self):
        revision, _ = self.accepted()
        new = self.revise(revision, amount="2000.00")
        result = self.flow()
        self.assertEqual(result["state"], "draft")
        self.assertEqual(result["snapshot"]["id"], str(new.pk))

    def test_multiple_requests_keep_independent_state_and_default_focus(self):
        self.decision()
        other = self.make_request()
        default = workflow_for(self.owner, self.team.pk, self.project.pk)
        self.assertEqual(default["request"].pk, other.pk)
        self.assertEqual(default["state"], "decision")
        self.assertEqual(self.flow()["state"], "draft")
        self.assertIn(str(self.item.pk), self.flow()["workspace_url"])

    def test_equal_created_times_have_deterministic_uuid_tie_break(self):
        now = timezone.now() + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=now):
            a = self.make_request()
            b = self.make_request()
        latest = workflow_for(self.owner, self.team.pk, self.project.pk)["request"]
        self.assertEqual(latest.pk, max(a.pk, b.pk))

    def test_malformed_explicit_focus_is_not_silently_ignored(self):
        for value in ("", "broken", "<script>alert(1)</script>"):
            with self.subTest(value=value), self.assertRaises(BadRequest):
                workflow_for(self.owner, self.team.pk, self.project.pk, value)

    def test_selector_has_no_domain_or_job_writes(self):
        models = (
            ClientRequest,
            AnalysisRun,
            AnalysisAttempt,
            AnalysisCharge,
            ChangeCard,
            ChangeRevision,
            ChangeEvent,
        )
        before = [m.objects.count() for m in models]
        with patch("mainapp.services.analysis.publish_run") as publish:
            for step in (None, 1, 2, 3, 4):
                workflow_for(
                    self.owner,
                    self.team.pk,
                    self.project.pk,
                    self.item.pk,
                    form_step=step,
                )
            publish.assert_not_called()
        self.assertEqual(before, [m.objects.count() for m in models])

    def test_list_uses_one_snapshot_statement_without_per_row_revision_reads(self):
        self.decision()
        for _ in range(5):
            self.decision(self.make_request())
        items = list(
            ClientRequest.objects.filter(
                scope_version__project=self.project
            ).select_related("scope_version")
        )
        with CaptureQueriesContext(connection) as queries:
            rows = describe_requests(self.owner, self.team.pk, self.project.pk, items)
            labels = [item.workflow_label for item in rows]
        card_selects = [q["sql"] for q in queries if "mainapp_changecard" in q["sql"]]
        self.assertEqual(len(card_selects), 1)
        self.assertEqual(len(labels), 6)
        self.assertTrue(all(label == "Черновик изменения" for label in labels))

    def test_mode_labels_never_construct_a_backend(self):
        with patch("mainapp.services.analysis_backends.get_backend") as backend:
            for mode, label in [
                ("disabled", "Ручной режим"),
                ("fixture", "Тестовый анализ — модель не подключена"),
                ("yandex", "Внешний анализ — только по вашему запуску"),
            ]:
                with override_settings(SCOPE_ANALYSIS_BACKEND=mode):
                    self.assertEqual(self.flow()["mode_label"], label)
            backend.assert_not_called()
