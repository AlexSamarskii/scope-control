import logging
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from allauth.account.models import EmailAddress

from mainapp.models import Team, TeamMembership
from mainapp.models.scope_control import Project, ScopeVersion
from mainapp.services.scope_control import (
    StaleScopeVersion,
    confirm_scope,
    create_project,
)
from usermodel.models import User


def verified_user(email):
    user = User.objects.create_user(email=email, password="a-long-test-password-795")
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ScopeTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = verified_user("owner-a@example.com")
        cls.outsider = verified_user("owner-b@example.com")
        cls.team = Team.objects.create(name="Studio A", slug="a")
        cls.other_team = Team.objects.create(name="Studio B", slug="b")
        cls.membership = TeamMembership.objects.create(
            team=cls.team, user=cls.owner, role="owner"
        )
        TeamMembership.objects.create(
            team=cls.other_team, user=cls.outsider, role="owner"
        )
        cls.project = create_project(cls.owner, cls.team.pk, name="Site A")
        cls.other_project = create_project(
            cls.outsider, cls.other_team.pk, name="Private Site B"
        )

    def url(self, name, **kwargs):
        return reverse(
            name, kwargs={"team_id": self.team.pk, "pk": self.project.pk, **kwargs}
        )

    def snapshot(self, **kwargs):
        return confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            body="  agreed scope\nsecond line  ",
            confirmed=True,
            **kwargs
        )


class TenantAndProjectTests(ScopeTestBase):
    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get(self.url("project_detail")).status_code, 302)

    def test_list_and_create_are_scoped_to_url_team(self):
        self.client.force_login(self.owner)
        url = reverse("project_list", kwargs={"team_id": self.team.pk})
        self.assertContains(self.client.get(url), "Site A")
        self.assertNotContains(self.client.get(url), "Private Site B")
        response = self.client.post(
            reverse("project_create", kwargs={"team_id": self.team.pk}),
            {"name": "Another A", "team": str(self.other_team.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Project.objects.get(name="Another A").team_id, self.team.pk)

    def test_foreign_read_and_writes_do_not_mutate(self):
        self.client.force_login(self.outsider)
        for name, payload in [
            ("project_detail", None),
            ("project_rename", {"name": "hacked"}),
            ("scope_confirm", {"body": "hacked", "confirmed": "on"}),
        ]:
            with self.subTest(name=name):
                response = (
                    self.client.get(self.url(name))
                    if payload is None
                    else self.client.post(self.url(name), payload)
                )
                self.assertEqual(response.status_code, 404)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Site A")
        self.assertFalse(self.project.versions.exists())

    def test_foreign_project_id_under_own_team_not_found(self):
        self.client.force_login(self.owner)
        url = reverse(
            "project_detail",
            kwargs={"team_id": self.team.pk, "pk": self.other_project.pk},
        )
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_version_id_from_another_project_is_not_accessible(self):
        other = confirm_scope(
            self.outsider,
            self.other_team.pk,
            self.other_project.pk,
            body="foreign",
            confirmed=True,
        )
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(self.url("scope_version", version_id=other.pk)).status_code,
            404,
        )

    def test_rename(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(
                self.url("project_rename"), {"name": "Changed"}
            ).status_code,
            302,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Changed")

    def test_member_and_viewer_can_read_but_cannot_write(self):
        self.client.force_login(self.owner)
        for role in ("member", "viewer"):
            self.membership.role = role
            self.membership.save()
            with self.subTest(role=role):
                self.assertEqual(
                    self.client.get(self.url("project_detail")).status_code, 200
                )
                self.assertEqual(
                    self.client.post(
                        self.url("scope_confirm"), {"body": "x", "confirmed": "on"}
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(
                        self.url("project_rename"), {"name": "hacked"}
                    ).status_code,
                    403,
                )
        self.assertFalse(self.project.versions.exists())

    def test_admin_can_confirm(self):
        self.membership.role = "admin"
        self.membership.save()
        self.assertEqual(self.snapshot().number, 1)

    def test_removed_or_expired_membership_loses_read_and_service_access(self):
        self.client.force_login(self.owner)
        self.membership.access_expires_at = timezone.now() - timezone.timedelta(
            seconds=1
        )
        self.membership.save()
        self.assertEqual(self.client.get(self.url("project_detail")).status_code, 404)
        from django.http import Http404

        with self.assertRaises(Http404):
            self.snapshot()
        self.membership.delete()
        self.assertEqual(self.client.get(self.url("project_detail")).status_code, 404)

    def test_inactive_team_loses_access(self):
        self.team.is_active = False
        self.team.save()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url("project_detail")).status_code, 404)

    def test_project_cannot_be_moved_to_other_team(self):
        self.project.team = self.other_team
        with self.assertRaises(ValidationError):
            self.project.save()
        with self.assertRaises(ValidationError):
            Project.objects.filter(pk=self.project.pk).update(
                team_id=self.other_team.pk
            )

    def test_domain_admin_scopes_queryset_and_is_readonly(self):
        version = self.snapshot()
        request = RequestFactory().get("/admin/")
        request.user = self.outsider
        project_admin = admin.site._registry[Project]
        version_admin = admin.site._registry[ScopeVersion]
        self.assertNotIn(self.project, project_admin.get_queryset(request))
        self.assertNotIn(version, version_admin.get_queryset(request))
        self.assertFalse(version_admin.has_view_permission(request, version))
        for method in (
            "has_add_permission",
            "has_change_permission",
            "has_delete_permission",
        ):
            self.assertFalse(getattr(version_admin, method)(request))

    def test_tenant_staff_cannot_use_inherited_global_admin(self):
        self.owner.is_staff = True
        self.owner.save()
        self.client.force_login(self.owner)
        self.assertNotEqual(self.client.get("/admin/mainapp/team/").status_code, 200)


class ScopeVersionTests(ScopeTestBase):
    def test_successor_keeps_exact_old_snapshot_and_source_project(self):
        first = self.snapshot()
        second = confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            expected_version=first.pk,
            body="new",
            exclusions="excluded",
            revision_rules="one round",
            confirmed=True,
        )
        first.refresh_from_db()
        self.assertEqual(first.body, "  agreed scope\nsecond line  ")
        self.assertEqual(second.previous_version_id, first.pk)
        self.assertEqual([second.number, first.number], [2, 1])
        self.client.force_login(self.owner)
        self.assertContains(
            self.client.get(self.url("scope_version", version_id=first.pk)),
            "agreed scope",
        )

    def test_model_and_queryset_mutations_are_denied(self):
        version = self.snapshot()
        version.body = "changed"
        operations = [
            lambda: version.save(),
            lambda: version.delete(),
            lambda: ScopeVersion.objects.filter(pk=version.pk).update(body="changed"),
            lambda: ScopeVersion.objects.filter(pk=version.pk).delete(),
            lambda: ScopeVersion.objects.bulk_update([version], ["body"]),
            lambda: ScopeVersion._base_manager.filter(pk=version.pk).update(
                body="changed"
            ),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ValidationError):
                operation()
        version.refresh_from_db()
        self.assertEqual(version.body, "  agreed scope\nsecond line  ")

    def test_arbitrary_creation_and_primary_key_overwrite_are_denied(self):
        version = self.snapshot()
        values = dict(
            project=self.project, number=4, body="forged", confirmed_by=self.owner
        )
        with self.assertRaises(ValidationError):
            ScopeVersion.objects.create(**values)
        forged = ScopeVersion(pk=version.pk, **values)
        forged._human_confirmation = True
        with self.assertRaises(ValidationError):
            forged.save()
        with self.assertRaises(ValidationError):
            ScopeVersion.objects.bulk_create([ScopeVersion(**values)])

    def test_cross_project_previous_version_is_rejected(self):
        version = self.snapshot()
        forged = ScopeVersion(
            project=self.other_project,
            number=2,
            previous_version=version,
            body="forged",
            confirmed_by=self.outsider,
        )
        forged._human_confirmation = True
        with self.assertRaises(ValidationError):
            forged.save()

    def test_confirmation_must_be_explicit(self):
        with self.assertRaises(ValidationError):
            confirm_scope(self.owner, self.team.pk, self.project.pk, body="x")
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(self.url("scope_confirm"), {"body": "x"}).status_code, 200
        )
        self.assertFalse(self.project.versions.exists())

    def test_preview_never_creates_a_version(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url("scope_confirm"), {"body": "preview", "action": "preview"}
        )
        self.assertContains(response, "preview")
        self.assertFalse(self.project.versions.exists())

    def test_repeated_or_stale_confirmation_is_conflict(self):
        self.client.force_login(self.owner)
        data = {"body": "agreed", "confirmed": "on"}
        self.assertEqual(
            self.client.post(self.url("scope_confirm"), data).status_code, 302
        )
        self.assertEqual(
            self.client.post(self.url("scope_confirm"), data).status_code, 409
        )
        self.assertEqual(self.project.versions.count(), 1)
        with self.assertRaises(StaleScopeVersion):
            self.snapshot()

    def test_blank_and_oversize_inputs_do_not_create_versions(self):
        self.client.force_login(self.owner)
        for body in ("   ", "x" * 100001):
            with self.subTest(length=len(body)):
                self.assertEqual(
                    self.client.post(
                        self.url("scope_confirm"), {"body": body, "confirmed": "on"}
                    ).status_code,
                    200,
                )
                with self.assertRaises(ValidationError):
                    confirm_scope(
                        self.owner,
                        self.team.pk,
                        self.project.pk,
                        body=body,
                        confirmed=True,
                    )
        self.assertFalse(self.project.versions.exists())

    def test_limits_preserve_valid_boundary_and_escape_html(self):
        prefix = "<script>alert('SC_TEST_MARKER')</script>"
        body = prefix + "x" * (100000 - len(prefix))
        version = confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            body=body,
            exclusions="x" * 20000,
            revision_rules="x" * 20000,
            confirmed=True,
        )
        self.client.force_login(self.owner)
        response = self.client.get(self.url("scope_version", version_id=version.pk))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(")
        self.assertEqual(version.body, body)

    def test_text_does_not_enter_technical_logs(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger()
        logger.addHandler(handler)
        try:
            self.client.force_login(self.owner)
            self.client.post(
                self.url("scope_confirm"),
                {"body": "SC_PRIVATE_LOG_MARKER", "confirmed": "on"},
            )
        finally:
            logger.removeHandler(handler)
        self.assertNotIn("SC_PRIVATE_LOG_MARKER", "\n".join(records))

    def test_project_and_company_deletion_protected(self):
        self.snapshot()
        with self.assertRaises(ProtectedError):
            self.project.delete()
        from mainapp.models import TeamDeletionBlocked

        with self.assertRaises(TeamDeletionBlocked):
            self.team.delete()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("team_delete", kwargs={"team_id": self.team.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.team.refresh_from_db()
        self.assertIsNone(self.team.deletion_scheduled_at)
        self.assertEqual(ScopeVersion.objects.count(), 1)
