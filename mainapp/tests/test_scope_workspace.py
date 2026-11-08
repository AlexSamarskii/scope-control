from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.http import Http404
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from mainapp.models.analysis import AnalysisRun, ClientRequest
from mainapp.models.scope_control import Project
from mainapp.services.analysis import create_request
from mainapp.services.demo_project import create_demo_project
from mainapp.services.project_workflow import workflow_for
from mainapp.services.scope_control import confirm_scope
from mainapp.services.workspace import (
    request_query,
    project_query,
    financial_summary,
    set_archived,
    describe_queue,
)
from mainapp.tests.test_scope_change_control import ChangeBase


class WorkspaceTests(ChangeBase):
    def workspace_url(self):
        return reverse("project_list", kwargs={"team_id": self.team.pk})

    def items(self, **kwargs):
        return list(request_query(self.owner, self.team.pk, **kwargs))

    def summary(self):
        return financial_summary(self.owner, self.team.pk)

    def archive(self, value=True):
        return set_archived(
            self.owner, self.team.pk, self.project.pk, archived=value, confirmed=True
        )

    def demo(self):
        return create_demo_project(self.owner, self.team.pk, confirmed=True)

    def test_queue_states_match_existing_workflow(self):
        cases = [
            ("included", "included"),
            ("insufficient_evidence", "unclear"),
            ("possible_change", "draft"),
        ]
        for category, expected in cases:
            item = self.make_request()
            args = (
                {}
                if category == "possible_change"
                else dict(amount=None, currency="", deadline_effect="")
            )
            self.decision(item, category=category, **args)
            row = request_query(self.owner, self.team.pk, state="all").get(pk=item.pk)
            flow = workflow_for(self.owner, self.team.pk, self.project.pk, item.pk)
            self.assertEqual(row.work_state, expected)
            self.assertEqual(row.work_state, flow["state"])
        self.assertEqual(self.items(state="decision")[0].work_state, "decision")

    def test_queue_shared_accepted_applied_and_single_action(self):
        rev = self.decision()
        self.shared(rev)
        row = self.items(state="shared")[0]
        self.assertEqual(
            describe_queue([row], self.team.pk, can_write=True)[0].next_action["url"],
            self.url("change_event", card_id=rev.card_id),
        )
        event = self.response(rev)
        row = self.items(state="accepted")[0]
        self.assertEqual(
            describe_queue([row], self.team.pk, can_write=True)[0].next_action["url"],
            self.url("change_apply", card_id=rev.card_id),
        )
        self.apply(rev, event)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.items(state="all")[0].work_state, "applied")

    def test_stale_accepted_must_not_suggest_apply(self):
        self.accepted()
        confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            body="New scope",
            expected_version=self.version.pk,
            confirmed=True,
        )
        row = self.items()[0]
        self.assertEqual(row.work_state, "stale")
        self.assertIn(
            "/requests/new/",
            describe_queue([row], self.team.pk, can_write=True)[0].next_action["url"],
        )
        self.assertEqual(self.summary()["rows"][0]["agreed"], Decimal("1500"))

    def test_revised_decision_resets_previous_answer_in_queue_and_sums(self):
        rev, event = self.accepted()
        self.revise(rev, amount="3000.25")
        row = self.items()[0]
        self.assertEqual(row.work_state, "draft")
        summary = self.summary()["rows"][0]
        self.assertEqual(summary["agreed"], Decimal("0"))
        self.assertEqual(summary["proposed"], Decimal("3000.25"))

    def test_money_separates_currency_zero_incomplete_and_rejection(self):
        self.decision(amount="0.00")
        second = self.make_request()
        self.decision(second, amount="0.10", currency="USD")
        third = self.make_request()
        self.decision(third, amount="0.20", currency="USD")
        unknown = self.make_request()
        self.decision(unknown, amount=None, currency="", deadline_effect="")
        missing_deadline = self.make_request()
        self.decision(missing_deadline, deadline_effect="")
        rejected = self.decision(self.make_request(), amount="9000.00")
        self.shared(rejected)
        self.response(rejected, kind="rejected")
        summary = self.summary()
        by_currency = {r["currency"]: r for r in summary["rows"]}
        self.assertEqual(by_currency["USD"]["proposed"], Decimal("0.30"))
        self.assertEqual(by_currency["RUB"]["proposed"], Decimal("0"))
        self.assertEqual(by_currency["RUB"]["proposed_count"], 1)
        self.assertEqual(summary["incomplete"], 2)
        self.assertEqual(len(self.items(state="decision")), 2)

    def test_accepted_and_applied_count_once(self):
        rev, event = self.accepted()
        self.assertEqual(self.summary()["rows"][0]["agreed"], Decimal("1500"))
        self.apply(rev, event)
        row = self.summary()["rows"][0]
        self.assertEqual(row["agreed"], Decimal("1500"))
        self.assertEqual(row["agreed_count"], 1)
        self.assertEqual(row["proposed"], 0)

    def test_stale_proposal_is_excluded_and_counted(self):
        self.decision()
        confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            body="New scope",
            expected_version=self.version.pk,
            confirmed=True,
        )
        summary = self.summary()
        self.assertEqual(summary["rows"], [])
        self.assertEqual(summary["stale"], 1)

    def test_search_name_body_source_and_completed_records(self):
        self.decision(category="included", amount=None, currency="", deadline_effect="")
        for term in ("site a", "Добавьте", "Синтетический"):
            self.assertEqual(
                [i.pk for i in self.items(query=term, state="all")], [self.item.pk]
            )
            self.assertEqual(
                list(project_query(self.owner, self.team.pk, query=term)),
                [self.project],
            )
        self.assertEqual(self.items(query="несуществующий"), [])
        self.client.force_login(self.owner)
        response = self.client.get(self.workspace_url(), {"q": "Добавьте"})
        self.assertEqual(response.context["queue"].paginator.count, 1)

    def test_archive_is_confirmed_idempotent_and_restorable_without_losing_history(
        self,
    ):
        rev = self.decision()
        with self.assertRaises(ValidationError):
            set_archived(self.owner, self.team.pk, self.project.pk, archived=True)
        original = self.archive().archived_at
        self.assertEqual(self.archive().archived_at, original)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.summary()["rows"], [])
        self.assertEqual(self.items(archive="archived")[0].pk, self.item.pk)
        self.assertEqual(self.items(archive="all")[0].pk, self.item.pk)
        self.assertEqual(Project.objects.get(pk=self.project.pk).versions.count(), 1)
        self.assertEqual(rev.card.current_revision.pk, rev.pk)
        self.client.force_login(self.owner)
        response = self.client.get(self.url("project_detail"))
        self.assertContains(response, "Проект в архиве")
        self.archive(False)
        self.assertEqual(self.items()[0].pk, self.item.pk)
        self.assertEqual(self.summary()["rows"][0]["proposed"], Decimal("1500"))

    def test_archive_get_post_csrf_and_missing_confirmation(self):
        self.client.force_login(self.owner)
        url = self.url("project_archive")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {"action": "archive"}).status_code, 200)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.archived_at)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(url, {"action": "archive", "confirmed": "on"}).status_code, 403
        )
        self.assertEqual(
            self.client.post(url, {"action": "archive", "confirmed": "on"}).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(url, {"action": "restore", "confirmed": "on"}).status_code,
            302,
        )
        self.project.refresh_from_db()
        self.assertIsNone(self.project.archived_at)

    def test_foreign_tenant_services_and_urls_fail(self):
        for call in (
            lambda: request_query(self.outsider, self.team.pk),
            lambda: financial_summary(self.outsider, self.team.pk),
            lambda: set_archived(
                self.owner,
                self.team.pk,
                self.other_project.pk,
                archived=True,
                confirmed=True,
            ),
            lambda: create_demo_project(self.outsider, self.team.pk, confirmed=True),
        ):
            with self.assertRaises(Http404):
                call()
        self.client.force_login(self.outsider)
        for url in (
            self.workspace_url(),
            self.url("project_archive"),
            reverse("demo_project", kwargs={"team_id": self.team.pk}),
        ):
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(
                self.client.post(
                    url, {"confirmed": "on", "action": "archive"}
                ).status_code,
                404,
            )

    def test_read_roles_can_search_but_cannot_archive_or_create_demo(self):
        for role in ("member", "viewer"):
            self.membership.role = role
            self.membership.save()
            self.client.force_login(self.owner)
            response = self.client.get(self.workspace_url())
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Посмотреть запрос")
            self.assertNotContains(
                response, self.url("change_create", request_id=self.item.pk)
            )
            with self.assertRaises(PermissionDenied):
                self.archive()
            with self.assertRaises(PermissionDenied):
                self.demo()
            self.assertEqual(
                self.client.post(
                    self.url("project_archive"),
                    {"action": "archive", "confirmed": "on"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    reverse("demo_project", kwargs={"team_id": self.team.pk}),
                    {"confirmed": "on"},
                ).status_code,
                403,
            )

    def test_expired_membership_blocks_all_new_services(self):
        self.membership.access_expires_at = timezone.now() - timedelta(seconds=1)
        self.membership.save()
        for call in (self.items, self.summary, self.archive, self.demo):
            with self.assertRaises(Http404):
                call()

    def test_deleted_projects_hidden_even_when_archived_all(self):
        from mainapp.services.data_lifecycle import preview_deletion, request_deletion

        token = preview_deletion(
            self.owner, self.team.pk, self.project.pk, name=self.project.name
        )
        request_deletion(
            self.owner,
            self.team.pk,
            self.project.pk,
            name=self.project.name,
            preview_token=token,
            submission_key=uuid4(),
            confirmed=True,
        )
        self.assertEqual(self.items(archive="all"), [])
        self.assertEqual(
            list(project_query(self.owner, self.team.pk, archive="all")), []
        )
        with self.assertRaises(Http404):
            self.archive()

    def test_demo_complete_and_practice_without_external_actions(self):
        with patch("mainapp.services.analysis.publish_run") as publish:
            demo = self.demo()
            publish.assert_not_called()
        self.assertTrue(demo.is_demo)
        self.assertEqual(demo.versions.count(), 2)
        items = ClientRequest.objects.filter(scope_version__project=demo)
        self.assertEqual(items.count(), 2)
        states = {
            workflow_for(self.owner, self.team.pk, demo.pk, item.pk)["state"]
            for item in items
        }
        self.assertEqual(states, {"applied", "decision"})
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertEqual(self.summary()["rows"], [])
        self.assertEqual([i.pk for i in self.items()], [self.item.pk])
        self.client.force_login(self.owner)
        for item in items:
            url = reverse(
                "request_detail",
                kwargs={"team_id": self.team.pk, "pk": demo.pk, "request_id": item.pk},
            )
            self.assertContains(
                self.client.get(url), "Учебный проект · вымышленные данные"
            )

    def test_demo_get_requires_confirmation_and_repeat_returns_existing(self):
        self.client.force_login(self.owner)
        url = reverse("demo_project", kwargs={"team_id": self.team.pk})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {}).status_code, 200)
        self.assertFalse(Project.objects.filter(is_demo=True).exists())
        first = self.client.post(url, {"confirmed": "on"})
        second = self.client.post(url, {"confirmed": "on"})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, second.url)
        self.assertEqual(Project.objects.filter(is_demo=True).count(), 1)
        demo = Project.objects.get(is_demo=True)
        set_archived(self.owner, self.team.pk, demo.pk, archived=True, confirmed=True)
        self.assertEqual(self.demo().pk, demo.pk)
        self.assertIsNotNone(self.demo().archived_at)

    def test_demo_marker_cannot_be_changed(self):
        demo = self.demo()
        demo.is_demo = False
        with self.assertRaises(ValidationError):
            demo.save()
        with self.assertRaises(ValidationError):
            Project.objects.filter(pk=demo.pk).update(is_demo=False)
        with self.assertRaises(ValidationError):
            Project.objects.bulk_update([demo], ["is_demo"])

    def test_demo_transaction_rolls_back_on_failure(self):
        with patch(
            "mainapp.services.demo_project.record_decision",
            side_effect=ValidationError("synthetic"),
        ):
            with self.assertRaises(ValidationError):
                self.demo()
        self.assertFalse(Project.objects.filter(is_demo=True).exists())
        self.assertEqual(ClientRequest.objects.count(), 1)

    def test_search_escapes_user_input_and_rejects_bad_filters(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            self.workspace_url(), {"q": '<script>alert("x")</script>'}
        )
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertContains(response, "&lt;script&gt;")
        for params in ({"q": "x" * 201}, {"archive": "bad"}, {"state": "bad"}):
            response = self.client.get(self.workspace_url(), params)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["search_form"].errors)
            self.assertEqual(response.context["queue"].paginator.count, 0)

    def test_queue_pagination_retains_filters_and_finance_is_not_page_total(self):
        self.decision()
        for _ in range(21):
            self.make_request()
        self.client.force_login(self.owner)
        response = self.client.get(
            self.workspace_url(), {"q": "Site A", "state": "all", "queue_page": 2}
        )
        self.assertEqual(response.context["queue"].number, 2)
        self.assertEqual(response.context["queue"].paginator.count, 22)
        self.assertContains(response, "q=Site+A")
        self.assertEqual(
            response.context["finance"]["rows"][0]["proposed"], Decimal("1500")
        )
        response = self.client.get(self.workspace_url(), {"q": "nothing"})
        self.assertEqual(
            response.context["finance"]["rows"][0]["proposed"], Decimal("1500")
        )

    def test_queue_does_not_query_per_row(self):
        for _ in range(4):
            self.decision(self.make_request())
        with CaptureQueriesContext(connection) as queries:
            rows = list(request_query(self.owner, self.team.pk, state="all")[:20])
            describe_queue(rows, self.team.pk, can_write=True)
            for row in rows:
                str(row.scope_version.project.name)
        self.assertLessEqual(len(queries), 3)

    def test_foreign_and_demo_money_never_leaks(self):
        demo = self.demo()
        self.client.force_login(self.owner)
        response = self.client.get(self.workspace_url())
        self.assertContains(response, "Согласование не означает оплату")
        self.assertNotContains(response, "Private Site B")
        self.assertNotContains(response, "12 000 ₽")
        self.assertEqual(response.context["finance"]["rows"], [])

    def test_whitespace_deadline_is_not_a_complete_estimate(self):
        self.decision(deadline_effect=" \t\n ")
        self.assertEqual(self.items()[0].work_state, "incomplete")
        self.assertEqual(self.summary()["rows"], [])
        self.assertEqual(self.summary()["incomplete"], 1)

    def test_search_terms_redacted_before_url_decoding_in_logs(self):
        import logging
        from project.settings import AuthenticationLinkFilter

        record = logging.LogRecord(
            "django.server",
            logging.INFO,
            "",
            0,
            "%s",
            ("GET /teams/x/projects/?q=private%20client%0Atext&state=all HTTP/1.1",),
            None,
        )
        AuthenticationLinkFilter().filter(record)
        self.assertNotIn("private", record.getMessage())
        self.assertNotIn("client", record.getMessage())
        self.assertIn("q=[redacted]&state=all", record.getMessage())
