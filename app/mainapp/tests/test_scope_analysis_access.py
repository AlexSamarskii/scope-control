from uuid import uuid4
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import RequestFactory
from django.utils import timezone
from mainapp.models.analysis import ClientRequest, AnalysisRun, AnalysisAttempt
from mainapp.services.analysis import (
    create_request,
    start_analysis,
    claim_run,
    finish_run,
    execute_run,
)
from mainapp.services.analysis_backends import fixture_response
from mainapp.services.scope_control import confirm_scope
from mainapp.tests.test_scope_analysis import AnalysisBase


class AnalysisAccessTests(AnalysisBase):
    def test_outsider_and_nested_ids_cannot_read_or_write(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 404)
        self.assertEqual(
            self.client.post(self.analyze_url(), {"start_key": uuid4()}).status_code,
            404,
        )
        self.client.force_login(self.owner)
        other_version = confirm_scope(
            self.outsider,
            self.other_team.pk,
            self.other_project.pk,
            body="private",
            confirmed=True,
        )
        data = dict(
            version=str(other_version.pk), body="attack", submission_key=uuid4()
        )
        self.assertEqual(
            self.client.post(self.url("request_create"), data).status_code, 200
        )
        with self.assertRaises(Http404):
            create_request(
                self.owner,
                self.team.pk,
                self.project.pk,
                version_id=other_version.pk,
                body="attack",
                submission_key=uuid4(),
            )
        bad = self.url("request_detail", request_id=self.item.pk).replace(
            str(self.project.pk), str(self.other_project.pk)
        )
        self.assertEqual(self.client.get(bad).status_code, 404)
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertEqual(ClientRequest.objects.count(), 1)

    def test_member_viewer_read_only_and_admin_can_start(self):
        self.client.force_login(self.owner)
        for role in ["member", "viewer"]:
            self.membership.role = role
            self.membership.save()
            self.assertEqual(self.client.get(self.detail_url()).status_code, 200)
            self.assertEqual(
                self.client.get(self.url("request_create")).status_code, 403
            )
            self.assertEqual(
                self.client.post(
                    self.analyze_url(), {"start_key": uuid4()}
                ).status_code,
                403,
            )
            with self.assertRaises(PermissionDenied):
                self.start()
        self.membership.role = "admin"
        self.membership.save()
        self.assertEqual(self.start().status, "queued")

    def test_revoked_expired_and_inactive_access_is_rechecked(self):
        self.client.force_login(self.owner)
        for state in ["expired", "inactive", "user_inactive", "removed"]:
            run = self.start()
            if state == "expired":
                self.membership.access_expires_at = timezone.now() - timezone.timedelta(
                    seconds=1
                )
                self.membership.save()
            elif state == "inactive":
                self.team.is_active = False
                self.team.save()
            elif state == "user_inactive":
                self.owner.is_active = False
                self.owner.save()
            else:
                self.membership.delete()
            execute_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.error_code, "access_revoked")
            self.assertEqual(run.attempt_count, 0)
            if state != "user_inactive":
                self.assertEqual(self.client.get(self.detail_url()).status_code, 404)
            if state == "removed":
                break
            self.membership.access_expires_at = None
            self.membership.save()
            self.team.is_active = True
            self.team.save()
            self.owner.is_active = True
            self.owner.save()

    def test_revocation_between_claim_and_finish_hides_result(self):
        run = self.start()
        claim = claim_run(run.pk)
        self.membership.role = "viewer"
        self.membership.save()
        self.assertTrue(
            finish_run(run.pk, claim.token, raw=fixture_response(claim.packet))
        )
        run.refresh_from_db()
        self.assertEqual(run.error_code, "access_revoked")
        self.assertIsNone(run.validated_result)
        self.assertEqual(run.attempts.get().outcome, "access_revoked")

    def test_admin_is_scoped_and_readonly_for_all_records(self):
        run = self.start()
        execute_run(run.pk)
        attempt = run.attempts.get()
        request = RequestFactory().get("/admin/")
        request.user = self.outsider
        for model, obj in [
            (ClientRequest, self.item),
            (AnalysisRun, run),
            (AnalysisAttempt, attempt),
        ]:
            model_admin = admin.site._registry[model]
            self.assertFalse(
                model_admin.get_queryset(request).filter(pk=obj.pk).exists()
            )
            self.assertFalse(model_admin.has_view_permission(request, obj))
            for action in [
                "has_add_permission",
                "has_change_permission",
                "has_delete_permission",
            ]:
                self.assertFalse(getattr(model_admin, action)(request))
            request.user = self.owner
            self.assertTrue(model_admin.has_view_permission(request, obj))
            request.user = self.outsider

    def test_task_unknown_id_and_id_only_contract(self):
        from mainapp.tasks.analysis import run_scope_analysis

        self.assertIsNone(run_scope_analysis(str(uuid4())))
        self.assertIsNone(run_scope_analysis("not-a-uuid"))
        with self.assertRaises(TypeError):
            run_scope_analysis(str(uuid4()), str(self.other_team.pk))
        with self.assertRaises(Http404):
            start_analysis(
                self.outsider,
                self.other_team.pk,
                self.other_project.pk,
                self.item.pk,
                start_key=uuid4(),
            )
        self.assertFalse(AnalysisRun.objects.exists())
