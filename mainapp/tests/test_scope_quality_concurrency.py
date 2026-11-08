from django.db import connection
from django.test import TransactionTestCase, skipUnlessDBFeature
from mainapp.models import TeamInvitation
from mainapp.services.invitation_delivery import claim_delivery, finish_delivery
from mainapp.tests import test_scope_control_concurrency as race_helpers


@skipUnlessDBFeature("has_select_for_update")
class DeliveryConcurrencyTests(TransactionTestCase):
    setUp = race_helpers.ConcurrencyTests.setUp
    concurrently = race_helpers.ConcurrencyTests.concurrently

    def test_duplicate_workers_claim_only_once(self):
        self.assertEqual(connection.vendor, "postgresql")
        invitation = TeamInvitation.objects.create(
            team=self.team, invited_by=self.owner, email="invite@example.com"
        )
        outcomes = self.concurrently(
            lambda: claim_delivery(invitation.pk), lambda: claim_delivery(invitation.pk)
        )
        self.assertEqual(sum(x is not None for x in outcomes), 1)
        self.assertEqual(
            TeamInvitation.objects.get(pk=invitation.pk).delivery_status, "sending"
        )

    def test_stale_completion_cannot_replace_current_attempt(self):
        invitation = TeamInvitation.objects.create(
            team=self.team, invited_by=self.owner, email="invite@example.com"
        )
        old = claim_delivery(invitation.pk)
        TeamInvitation.objects.filter(pk=invitation.pk).update(
            delivery_status="pending"
        )
        current = claim_delivery(invitation.pk)
        self.concurrently(
            lambda: finish_delivery(old, "failed"),
            lambda: finish_delivery(current, "sent"),
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.delivery_status, "sent")
        self.assertEqual(invitation.delivery_attempt, current.delivery_attempt)
