from django.db import connection
from django.test import TransactionTestCase, skipUnlessDBFeature

from mainapp.models.scope_control import Project
from mainapp.models.analysis import ClientRequest
from mainapp.services.demo_project import create_demo_project
from mainapp.services.workspace import set_archived
from mainapp.tests import test_scope_control_concurrency as race_helpers
from usermodel.models import User


@skipUnlessDBFeature("has_select_for_update")
class WorkspaceConcurrencyTests(TransactionTestCase):
    setUp = race_helpers.ConcurrencyTests.setUp
    concurrently = race_helpers.ConcurrencyTests.concurrently

    def test_concurrent_demo_creation_returns_one_complete_project(self):
        self.assertEqual(connection.vendor, "postgresql")

        def create():
            return create_demo_project(
                User.objects.get(pk=self.owner.pk), self.team.pk, confirmed=True
            ).pk

        results = self.concurrently(create, create)
        self.assertEqual(results[0], results[1])
        demo = Project.objects.get(is_demo=True, team=self.team)
        self.assertEqual(demo.versions.count(), 2)
        self.assertEqual(
            ClientRequest.objects.filter(scope_version__project=demo).count(), 2
        )

    def test_duplicate_archive_does_not_reset_timestamp_or_delete(self):
        def archive():
            return set_archived(
                User.objects.get(pk=self.owner.pk),
                self.team.pk,
                self.project.pk,
                archived=True,
                confirmed=True,
            ).archived_at

        results = self.concurrently(archive, archive)
        self.assertEqual(results[0], results[1])
        self.assertTrue(
            Project.objects.filter(pk=self.project.pk, archived_at=results[0]).exists()
        )
