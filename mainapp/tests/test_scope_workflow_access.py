from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import Client
from django.urls import reverse
from django.utils import timezone
from mainapp.models.analysis import ClientRequest, AnalysisRun, AnalysisAttempt
from mainapp.models.analysis_budget import AnalysisCharge
from mainapp.models.change_control import ChangeCard, ChangeRevision, ChangeEvent
from mainapp.models.scope_control import Project, ScopeVersion
from mainapp.services.analysis import create_request
from mainapp.services.data_lifecycle import preview_deletion, request_deletion
from mainapp.tests.test_scope_change_control import ChangeBase


class WorkflowAccessTests(ChangeBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_project_page_focus_default_explicit_and_other_page(self):
        other = self.make_request()
        self.decision()
        url = self.url("project_detail")
        response = self.client.get(url)
        self.assertEqual(response.context["workflow"]["request"].pk, other.pk)
        response = self.client.get(
            url, {"focus": self.item.pk, "requests_page": 2, "changes_page": 1}
        )
        self.assertEqual(response.context["workflow"]["request"].pk, self.item.pk)
        self.assertEqual(response.context["workflow"]["state"], "draft")
        self.assertIn(str(self.item.pk), response.context["request_page_prefix"])
        self.assertIn("changes_page=1", response.context["request_page_prefix"])
        self.assertIn("requests_page=2", response.context["change_page_prefix"])

    def test_focus_validation_and_foreign_project_or_team(self):
        url = self.url("project_detail")
        self.assertEqual(self.client.get(url, {"focus": "bad"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"focus": uuid4()}).status_code, 404)
        from mainapp.services.scope_control import confirm_scope

        version = confirm_scope(
            self.outsider,
            self.other_team.pk,
            self.other_project.pk,
            body="Private",
            confirmed=True,
        )
        item = create_request(
            self.outsider,
            self.other_team.pk,
            self.other_project.pk,
            version_id=version.pk,
            body="foreign-secret-marker",
            submission_key=uuid4(),
        )
        response = self.client.get(url, {"focus": item.pk})
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "foreign-secret-marker", status_code=404)
        response = self.client.get(
            reverse(
                "project_detail",
                kwargs={"team_id": self.team.pk, "pk": self.other_project.pk},
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_every_role_sees_data_but_write_actions_only_owner_admin(self):
        for role in ("owner", "admin", "member", "viewer"):
            self.membership.role = role
            self.membership.save()
            response = self.client.get(
                self.url("project_detail"), {"focus": self.item.pk}
            )
            self.assertEqual(response.status_code, 200)
            workflow = response.context["workflow"]
            self.assertEqual(workflow["can_write"], role in ("owner", "admin"))
            create_url = self.url("change_create", request_id=self.item.pk)
            if role in ("owner", "admin"):
                self.assertContains(response, f'href="{create_url}"')
            else:
                self.assertNotContains(response, f'href="{create_url}"')
                self.assertContains(
                    response, "Изменения сохраняет владелец или менеджер"
                )

    def test_expired_revoked_inactive_and_foreign_user_denied(self):
        url = self.url("project_detail")
        self.membership.access_expires_at = timezone.now() - timedelta(seconds=1)
        self.membership.save()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.membership.access_expires_at = None
        self.membership.save()
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertIn(self.client.get(url).status_code, (302, 404))
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.membership.delete()
        self.owner.is_active = True
        self.owner.save(update_fields=["is_active"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_pending_deletion_fences_new_guide_and_children(self):
        revision = self.decision()
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
        for name, extra in [
            ("project_detail", {}),
            ("scope_version", {"version_id": self.version.pk}),
            ("request_detail", {"request_id": self.item.pk}),
            ("change_detail", {"card_id": revision.card_id}),
        ]:
            self.assertEqual(self.client.get(self.url(name, **extra)).status_code, 404)

    def test_old_version_route_is_explicit_and_preserves_old_content(self):
        from mainapp.services.scope_control import confirm_scope

        new = confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            body="New scope",
            expected_version=self.version.pk,
            confirmed=True,
        )
        response = self.client.get(
            self.url("scope_version", version_id=self.version.pk)
        )
        self.assertContains(response, "Просмотр сохранённой версии 1")
        self.assertContains(response, "Текущая версия проекта — 2")
        self.assertContains(response, self.version.body)
        self.assertNotContains(response, 'id="workflow-title"')
        self.assertEqual(response.context["selected_version"].pk, self.version.pk)

    def test_source_text_is_escaped_in_guide_and_rows(self):
        body = "<script>private-xss-marker</script>" + ("Z" * 2000)
        item = create_request(
            self.owner,
            self.team.pk,
            self.project.pk,
            version_id=self.version.pk,
            body=body,
            submission_key=uuid4(),
        )
        response = self.client.get(self.url("project_detail"), {"focus": item.pk})
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>private-xss-marker")
        self.assertNotContains(response, "Z" * 2000)

    def test_get_pages_do_not_mutate_or_publish_jobs(self):
        revision = self.decision()
        models = (
            Project,
            ScopeVersion,
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
            for name, extra in [
                ("project_detail", {}),
                ("scope_confirm", {}),
                ("request_create", {}),
                ("request_detail", {"request_id": self.item.pk}),
                ("change_detail", {"card_id": revision.card_id}),
                ("change_revise", {"card_id": revision.card_id}),
                ("change_event", {"card_id": revision.card_id}),
            ]:
                self.assertEqual(
                    self.client.get(self.url(name, **extra)).status_code, 200
                )
            publish.assert_not_called()
        self.assertEqual(before, [m.objects.count() for m in models])

    def test_shared_form_keeps_csrf_and_unchecked_confirmation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        url = self.url("change_create", request_id=self.item.pk)
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["form"].fields["confirmed"].initial)
        self.assertEqual(client.post(url, {}).status_code, 403)

    def test_request_and_card_share_context_while_forms_show_current_step(self):
        response = self.client.get(self.detail_url())
        self.assertEqual(response.context["workflow"]["request"].pk, self.item.pk)
        revision = self.decision()
        response = self.client.get(self.url("change_detail", card_id=revision.card_id))
        self.assertEqual(response.context["workflow"]["state"], "draft")
        response = self.client.get(self.url("change_revise", card_id=revision.card_id))
        self.assertEqual(response.context["workflow"]["form_step"], 3)
        self.assertEqual(
            [
                s["number"]
                for s in response.context["workflow"]["steps"]
                if s["current"]
            ],
            [3],
        )

    def test_pagination_is_bounded_and_focus_can_be_off_page(self):
        for _ in range(26):
            self.make_request()
        response = self.client.get(self.url("project_detail"), {"focus": self.item.pk})
        self.assertEqual(len(response.context["client_requests"]), 25)
        self.assertEqual(response.context["workflow"]["request"].pk, self.item.pk)
        self.assertContains(response, f"focus={self.item.pk}&amp;requests_page=2")
