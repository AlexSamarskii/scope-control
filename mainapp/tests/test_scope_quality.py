from datetime import timedelta
from io import BytesIO, StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings, RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.views.defaults import server_error
from mainapp.models import TeamInvitation
from mainapp.services.invitation_delivery import (
    enqueue_invitation,
    claim_delivery,
    finish_delivery,
    request_resend,
)
from mainapp.tasks.teams import send_team_invitation_email
from mainapp.tests.test_scope_change_control import ChangeBase
from mainapp.forms.change_control import DecisionForm, ChangeEventForm
from mainapp.services.change_exports import build_draft
from mainapp.services.scope_diff import scope_diff


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_DELIVERABILITY_CHECK=False,
    POST_OFFICE={
        "BACKENDS": {"default": "django.core.mail.backends.locmem.EmailBackend"},
        "CELERY_ENABLED": False,
    },
    DEFAULT_FROM_EMAIL="no-reply@example.com",
)
class QualityTests(ChangeBase):
    def setUp(self):
        self.client.force_login(self.owner)

    def invitation(self, **kwargs):
        return TeamInvitation.objects.create(
            team=self.team,
            invited_by=self.owner,
            email="quality-invite@example.com",
            role="member",
            **kwargs
        )

    def resend_url(self, invitation):
        return reverse(
            "resend_invitation",
            kwargs={"team_id": self.team.pk, "invitation_id": invitation.pk},
        )

    def test_broker_failure_durable_and_recovery_sends_once(self):
        url = reverse("invite_member", kwargs={"team_id": self.team.pk})
        with patch(
            "mainapp.services.invitation_delivery.current_app.send_task",
            side_effect=RuntimeError("offline"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    url, {"email": "quality-invite@example.com", "role": "member"}
                )
        self.assertEqual(response.status_code, 302)
        invitation = TeamInvitation.objects.get(email="quality-invite@example.com")
        self.assertEqual(invitation.delivery_status, "pending")
        with patch(
            "mainapp.services.invitation_delivery.current_app.send_task"
        ) as dispatch:
            call_command("reconcile_scope_invitations", stdout=StringIO())
            dispatch.assert_called_once()
        send_team_invitation_email(invitation.pk)
        send_team_invitation_email(invitation.pk)
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, "sent")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].content_subtype, "html")
        self.assertIn("Участник — чтение", mail.outbox[0].body)
        self.assertEqual(
            TeamInvitation.objects.filter(email=invitation.email).count(), 1
        )

    def test_failed_smtp_is_visible_without_automatic_retransmission(self):
        invitation = self.invitation()
        with patch("mainapp.tasks.teams.mail.send", side_effect=TimeoutError):
            send_team_invitation_email(invitation.pk)
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, "failed")
        with patch(
            "mainapp.services.invitation_delivery.current_app.send_task"
        ) as dispatch:
            call_command("reconcile_scope_invitations", stdout=StringIO())
            dispatch.assert_not_called()
        with self.assertRaises(ValidationError):
            request_resend(invitation.pk, self.team.pk)
        TeamInvitation.objects.filter(pk=invitation.pk).update(
            delivery_updated_at=timezone.now() - timedelta(minutes=3)
        )
        with patch(
            "mainapp.services.invitation_delivery.current_app.send_task"
        ) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.resend_url(invitation))
            self.assertEqual(response.status_code, 302)
            dispatch.assert_called_once()
        send_team_invitation_email(invitation.pk)
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, "sent")
        self.assertEqual(len(mail.outbox), 1)

    def test_no_delivery_for_revoked_expired_inactive_or_accepted(self):
        invitation = self.invitation()
        for status in ["revoked", "accepted", "declined"]:
            TeamInvitation.objects.filter(pk=invitation.pk).update(status=status)
            self.assertIsNone(claim_delivery(invitation.pk))
        TeamInvitation.objects.filter(pk=invitation.pk).update(
            status="pending", expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertIsNone(claim_delivery(invitation.pk))
        TeamInvitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now() + timedelta(days=1)
        )
        self.team.is_active = False
        self.team.save(update_fields=["is_active"])
        self.assertIsNone(claim_delivery(invitation.pk))

    def test_old_attempt_cannot_finish_new_delivery(self):
        invitation = self.invitation()
        old = claim_delivery(invitation.pk)
        self.assertIsNone(claim_delivery(invitation.pk))
        TeamInvitation.objects.filter(pk=invitation.pk).update(
            delivery_status="pending", delivery_attempt=None
        )
        new = claim_delivery(invitation.pk)
        finish_delivery(old, "sent")
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, "sending")
        self.assertEqual(invitation.delivery_attempt, new.delivery_attempt)

    def test_resend_access_and_methods(self):
        invitation = self.invitation(
            delivery_updated_at=timezone.now() - timedelta(minutes=3)
        )
        self.assertEqual(self.client.get(self.resend_url(invitation)).status_code, 405)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(self.resend_url(invitation)).status_code, 404)
        self.client.force_login(self.owner)
        for role in ["member", "viewer"]:
            self.membership.role = role
            self.membership.save()
            self.assertEqual(
                self.client.post(self.resend_url(invitation)).status_code, 403
            )
        self.membership.role = "admin"
        self.membership.save()
        invitation.role = "admin"
        invitation.save()
        self.assertEqual(self.client.post(self.resend_url(invitation)).status_code, 403)

    def test_role_labels_do_not_promise_member_writes(self):
        response = self.client.get(
            reverse("invite_member", kwargs={"team_id": self.team.pk})
        )
        self.assertContains(response, "Участник — чтение")
        self.assertNotContains(response, "Create and edit")
        self.membership.role = "member"
        self.membership.save()
        self.assertEqual(
            self.client.get(
                reverse("project_create", kwargs={"team_id": self.team.pk})
            ).status_code,
            403,
        )

    def test_avatar_and_logo_upload_authorized_delivery_and_clear(self):
        def upload():
            buf = BytesIO()
            Image.new("RGB", (24, 24), (80, 90, 180)).save(buf, format="PNG")
            return SimpleUploadedFile(
                "quality.png", buf.getvalue(), content_type="image/png"
            )

        with TemporaryDirectory(
            prefix="scope-quality-media-"
        ) as directory, override_settings(MEDIA_ROOT=directory):
            with patch("mainapp.webhooks.business_events.on_user_profile_updated"):
                response = self.client.post(
                    reverse("account_profile"),
                    {
                        "first_name": "Test",
                        "last_name": "",
                        "profile_picture": upload(),
                    },
                )
            self.assertEqual(response.status_code, 302)
            for name in ["profile_avatar", "profile_avatar_thumbnail"]:
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/jpeg")
                self.assertIn("no-store", response["Cache-Control"])
            response = self.client.post(
                reverse("team_settings", kwargs={"team_id": self.team.pk}),
                {
                    "name": self.team.name,
                    "slug": self.team.slug,
                    "timezone": "Europe/Moscow",
                    "logo": upload(),
                },
            )
            self.assertEqual(response.status_code, 302)
            logo = reverse("team_logo", kwargs={"team_id": self.team.pk})
            self.assertEqual(self.client.get(logo)["Content-Type"], "image/jpeg")
            self.client.force_login(self.outsider)
            self.assertEqual(self.client.get(logo).status_code, 404)
            self.assertEqual(
                self.client.get(reverse("profile_avatar"))["Content-Type"],
                "image/svg+xml",
            )
            self.client.logout()
            self.assertEqual(self.client.get(logo).status_code, 302)
            self.client.force_login(self.owner)
            with patch("mainapp.webhooks.business_events.on_user_profile_updated"):
                self.assertEqual(
                    self.client.post(
                        reverse("account_profile"),
                        {
                            "first_name": "Test",
                            "last_name": "",
                            "profile_picture-clear": "on",
                        },
                    ).status_code,
                    302,
                )
            self.assertEqual(
                self.client.get(reverse("profile_avatar"))["Content-Type"],
                "image/svg+xml",
            )
            self.assertEqual(
                self.client.get("/media/../../private-media/test").status_code, 404
            )

    def test_image_bounds(self):
        from mainapp.forms.profile_images import validate_display_image

        value = SimpleUploadedFile(
            "large.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png"
        )
        with self.assertRaises(ValidationError):
            validate_display_image(value)
        with self.assertRaises(ValidationError):
            validate_display_image(SimpleUploadedFile("bad.png", b"not image"))

    def test_safe_error_pages(self):
        self.assertContains(
            self.client.get("/not-a-real-page/"),
            "К доступным проектам",
            status_code=404,
        )
        self.membership.role = "member"
        self.membership.save()
        self.assertContains(
            self.client.get(
                reverse("project_create", kwargs={"team_id": self.team.pk})
            ),
            "Недостаточно прав",
            status_code=403,
        )
        response = server_error(RequestFactory().get("/"))
        self.assertEqual(response.status_code, 500)
        self.assertIn("К доступным проектам", response.content.decode())
        self.assertNotIn("Traceback", response.content.decode())

    def test_server_rejects_irrelevant_fields(self):
        form = DecisionForm(
            item=self.item,
            data={
                "submission_key": uuid4(),
                "category": "included",
                "description": "Text",
                "rationale": "Reason",
                "amount": "10",
                "currency": "RUB",
                "confirmed": True,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("category", form.errors)
        form = ChangeEventForm(
            data={
                "submission_key": uuid4(),
                "kind": "shared",
                "sender_name": "unexpected",
                "confirmed": True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("sender_name", form.errors)

    def test_clean_draft_copy_controls_and_no_mutation(self):
        revision = self.decision()
        text = build_draft(revision)
        self.assertNotIn(str(self.item.pk), text)
        self.assertNotIn("Приложение", text)
        self.assertIn(revision.description, text)
        self.assertIn(revision.amount_label, text)
        before = revision.card.events.count()
        response = self.client.get(self.url("change_detail", card_id=revision.card_id))
        self.assertContains(response, 'data-copy-target="#client-draft"')
        self.assertEqual(revision.card.events.count(), before)

    def test_prefill_diff_is_escaped_and_does_not_confirm(self):
        revision, event = self.accepted()
        url = self.url("change_apply", card_id=revision.card_id)
        response = self.client.get(url)
        form = response.context["form"]
        self.assertEqual(form["body"].value(), self.version.body)
        self.assertEqual(form["exclusions"].value(), self.version.exclusions)
        payload = {
            **self.apply_data(revision, event),
            "body": "<script>audit-xss</script>",
            "action": "preview",
            "confirmed": "on",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Что изменится")
        self.assertContains(response, "&lt;script&gt;audit-xss&lt;/script&gt;")
        self.assertNotContains(response, "<script>audit-xss</script>")
        self.assertFalse(response.context["form"]["confirmed"].value())
        self.assertEqual(self.project.versions.count(), 1)

    def test_diff_bounds_and_unchanged_fields(self):
        result = scope_diff(
            self.version,
            {
                "body": "x\n" * 1200,
                "exclusions": self.version.exclusions,
                "revision_rules": self.version.revision_rules,
            },
        )
        self.assertLessEqual(len(result[0]["changes"]), 2)
        self.assertFalse(result[1]["changed"])

    def test_diff_ignores_browser_crlf_but_highlights_added_line(self):
        from types import SimpleNamespace

        source = SimpleNamespace(body="First\nSecond", exclusions="", revision_rules="")
        result = scope_diff(source, {"body": "First\r\nSecond\r\nAdded"})
        self.assertEqual(
            result[0]["changes"], [("same", "First\nSecond"), ("added", "Added")]
        )
