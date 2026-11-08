from io import StringIO
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mainapp.models import Team, TeamInvitation, TeamMembership
from usermodel.models import User


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    EMAIL_DELIVERABILITY_CHECK=False,
    SIGNUP_EMAIL_MX_CHECK=False,
)
class InvitationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com")
        self.team = Team.objects.create(name="Studio", slug="studio")
        TeamMembership.objects.create(team=self.team, user=self.owner, role="owner")
        self.user = User.objects.create_user(
            email="recipient@example.com", password="long-test-password-891"
        )
        self.invite = TeamInvitation.objects.create(
            team=self.team, invited_by=self.owner, email=self.user.email
        )

    def verify(self, user):
        return EmailAddress.objects.create(
            user=user, email=user.email, primary=True, verified=True
        )

    def test_only_verified_recipient_can_accept(self):
        with self.assertRaises(ValidationError):
            self.invite.accept(self.user)
        stranger = User.objects.create_user(email="stranger@example.com")
        self.verify(stranger)
        with self.assertRaises(ValidationError):
            self.invite.accept(stranger)
        self.verify(self.user)
        membership = self.invite.accept(self.user)
        self.assertEqual(membership.team_id, self.team.pk)
        self.assertEqual(membership.user_id, self.user.pk)
        with self.assertRaises(ValidationError):
            self.invite.accept(self.user)
        self.assertEqual(
            TeamMembership.objects.filter(user=self.user, team=self.team).count(), 1
        )

    def test_automatic_expiration_is_set_on_uuid_model(self):
        self.assertIsNotNone(self.invite.expires_at)
        self.assertGreater(self.invite.expires_at, timezone.now())

    def test_expired_revoked_or_inactive_invites_cannot_grant_access(self):
        self.verify(self.user)
        for status in ("expired", "revoked"):
            self.invite.status = status
            self.invite.save()
            with self.assertRaises(ValidationError):
                self.invite.accept(self.user)
        self.invite.status = "pending"
        self.invite.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.invite.save()
        with self.assertRaises(ValidationError):
            self.invite.accept(self.user)
        self.invite.expires_at = timezone.now() + timezone.timedelta(days=1)
        self.invite.save()
        self.team.is_active = False
        self.team.save()
        with self.assertRaises(ValidationError):
            self.invite.accept(self.user)
        self.assertFalse(
            TeamMembership.objects.filter(user=self.user, team=self.team).exists()
        )

    def test_http_wrong_user_and_replay_do_not_create_membership(self):
        self.verify(self.owner)
        self.client.force_login(self.owner)
        url = reverse("accept_invitation", kwargs={"token": self.invite.token})
        self.client.post(url)
        self.assertFalse(TeamMembership.objects.filter(user=self.user).exists())
        self.verify(self.user)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.client.post(url)
        self.assertEqual(TeamMembership.objects.filter(user=self.user).count(), 1)

    def test_decline_requires_verified_recipient(self):
        url = reverse("decline_invitation", kwargs={"token": self.invite.token})
        self.assertEqual(self.client.post(url).status_code, 302)
        self.verify(self.owner)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, "pending")

    def test_signup_without_invitation_is_closed(self):
        self.client.post(
            reverse("account_signup"),
            {"email": "new@example.com", "password1": "strong-test-pass-761"},
        )
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def new_invitation(self):
        return TeamInvitation.objects.create(
            team=self.team, invited_by=self.owner, email="new@example.com"
        )

    def test_signup_email_is_fixed_and_mismatch_rejected(self):
        invitation = self.new_invitation()
        self.client.get(
            reverse("accept_invitation", kwargs={"token": invitation.token})
        )
        response = self.client.get(reverse("account_signup"))
        self.assertContains(response, 'value="new@example.com"')
        self.assertContains(response, "readonly")
        response = self.client.post(
            reverse("account_signup"),
            {
                "email": "attacker@example.com",
                "password1": "strong-test-pass-761",
                "tos": "on",
                "dpa": "on",
            },
        )
        self.assertLess(response.status_code, 500)
        self.assertFalse(User.objects.filter(email="attacker@example.com").exists())

    def test_signup_creates_unverified_account_without_membership(self):
        invitation = self.new_invitation()
        self.client.get(
            reverse("accept_invitation", kwargs={"token": invitation.token})
        )
        response = self.client.post(
            reverse("account_signup"),
            {
                "email": "new@example.com",
                "password1": "strong-test-pass-761",
                "tos": "on",
                "dpa": "on",
            },
        )
        self.assertLess(response.status_code, 400)
        user = User.objects.get(email="new@example.com")
        self.assertFalse(EmailAddress.objects.filter(user=user, verified=True).exists())
        self.assertFalse(TeamMembership.objects.filter(user=user).exists())
        with self.assertRaises(ValidationError):
            invitation.accept(user)

    def test_revoked_invite_in_existing_signup_session_is_rejected(self):
        invitation = self.new_invitation()
        self.client.get(
            reverse("accept_invitation", kwargs={"token": invitation.token})
        )
        invitation.revoke()
        self.client.post(
            reverse("account_signup"),
            {"email": "new@example.com", "password1": "strong-test-pass-761"},
        )
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_native_email_confirmation_then_invitation_acceptance(self):
        from allauth.account.models import EmailConfirmationHMAC

        invitation = self.new_invitation()
        join_url = reverse("accept_invitation", kwargs={"token": invitation.token})
        self.client.get(join_url)
        self.client.post(
            reverse("account_signup"),
            {
                "email": "new@example.com",
                "password1": "strong-test-pass-761",
                "tos": "on",
                "dpa": "on",
            },
        )
        user = User.objects.get(email="new@example.com")
        address = EmailAddress.objects.get(user=user, email=user.email)
        confirmation = EmailConfirmationHMAC(address)
        response = self.client.post(
            reverse("account_confirm_email", kwargs={"key": confirmation.key})
        )
        self.assertLess(response.status_code, 400)
        address.refresh_from_db()
        self.assertTrue(address.verified)
        self.client.post(
            reverse("account_login"),
            {"login": user.email, "password": "strong-test-pass-761"},
        )
        response = self.client.post(join_url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            TeamMembership.objects.filter(user=user, team=self.team).exists()
        )

    def test_bootstrap_prompts_without_logging_password_and_is_idempotent(self):
        output = StringIO()
        password = "bootstrap-test-long-password-832"
        with patch(
            "speedpycom.management.commands.bootstrap_scope_control.getpass",
            return_value=password,
        ) as prompt:
            call_command(
                "bootstrap_scope_control",
                email="first@example.com",
                team="First",
                slug="first",
                stdout=output,
            )
            self.assertEqual(prompt.call_count, 2)
        user = User.objects.get(email="first@example.com")
        self.assertTrue(user.check_password(password))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertNotIn(password, output.getvalue())
        with patch(
            "speedpycom.management.commands.bootstrap_scope_control.getpass"
        ) as prompt:
            call_command(
                "bootstrap_scope_control",
                email="first@example.com",
                team="First",
                slug="first",
                stdout=output,
            )
            prompt.assert_not_called()
        self.assertEqual(Team.objects.filter(slug="first").count(), 1)
