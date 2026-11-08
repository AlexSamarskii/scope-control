from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, skipUnlessDBFeature

from mainapp.models import Team, TeamInvitation, TeamMembership
from mainapp.models.scope_control import ScopeVersion
from mainapp.services.scope_control import (
    StaleScopeVersion,
    confirm_scope,
    create_project,
)
from usermodel.models import User


@skipUnlessDBFeature("has_select_for_update")
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com")
        self.team = Team.objects.create(name="Concurrent", slug="concurrent")
        TeamMembership.objects.create(user=self.owner, team=self.team, role="owner")
        self.project = create_project(
            self.owner, self.team.pk, name="Concurrent project"
        )

    def concurrently(self, left, right):
        barrier = Barrier(2)

        def invoke(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(invoke, operation) for operation in (left, right)
            ]
            return [future.result(timeout=25) for future in futures]

    def confirm(self, expected_version=None):
        try:
            user = User.objects.get(pk=self.owner.pk)
            confirm_scope(
                user,
                self.team.pk,
                self.project.pk,
                expected_version=expected_version,
                body="agreed",
                confirmed=True,
            )
            return "confirmed"
        except StaleScopeVersion:
            return "stale"

    def test_two_simultaneous_first_confirmations_create_one_version(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.assertEqual(
            sorted(self.concurrently(self.confirm, self.confirm)),
            ["confirmed", "stale"],
        )
        self.assertEqual(ScopeVersion.objects.count(), 1)

    def test_two_successors_have_no_lost_update(self):
        first = confirm_scope(
            self.owner, self.team.pk, self.project.pk, body="v1", confirmed=True
        )
        self.assertEqual(
            sorted(
                self.concurrently(
                    lambda: self.confirm(first.pk), lambda: self.confirm(first.pk)
                )
            ),
            ["confirmed", "stale"],
        )
        self.assertEqual(
            list(self.project.versions.values_list("number", flat=True)), [2, 1]
        )

    def invitation(self):
        user = User.objects.create_user(email="invited@example.com")
        EmailAddress.objects.create(user=user, email=user.email, verified=True)
        invite = TeamInvitation.objects.create(
            team=self.team, invited_by=self.owner, email=user.email
        )
        return user, invite

    def test_concurrent_invitation_acceptance_is_single_use(self):
        user, invite = self.invitation()

        def accept():
            try:
                TeamInvitation.objects.get(pk=invite.pk).accept(
                    User.objects.get(pk=user.pk)
                )
                return "accepted"
            except ValidationError:
                return "rejected"

        self.assertEqual(
            sorted(self.concurrently(accept, accept)), ["accepted", "rejected"]
        )
        self.assertEqual(
            TeamMembership.objects.filter(team=self.team, user=user).count(), 1
        )

    def test_accept_and_revoke_have_one_consistent_outcome(self):
        user, invite = self.invitation()

        def accept():
            try:
                TeamInvitation.objects.get(pk=invite.pk).accept(
                    User.objects.get(pk=user.pk)
                )
            except ValidationError:
                pass

        self.concurrently(
            accept, lambda: TeamInvitation.objects.get(pk=invite.pk).revoke()
        )
        invite.refresh_from_db()
        self.assertIn(invite.status, ["accepted", "revoked"])
        self.assertEqual(
            TeamMembership.objects.filter(team=self.team, user=user).exists(),
            invite.status == "accepted",
        )
