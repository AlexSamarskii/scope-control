from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import (
    RequestFactory,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.utils import timezone

from mainapp.models import Team, TeamMembership
from mainapp.models.analysis import AnalysisRun, AnalysisAttempt, _domain_write
from mainapp.models.analysis_budget import AnalysisCharge, ProviderBudget, GLOBAL_LIMIT
from mainapp.services import analysis_budget as money
from mainapp.services.analysis import (
    claim_run,
    finish_run,
    mark_generation,
    record_receipt,
    reconcile_analysis,
    start_analysis,
    create_request,
)
from mainapp.services.analysis_provider import ProviderReply, RESERVE_MICRORUB
from mainapp.services.scope_control import create_project, confirm_scope
from mainapp.tests.test_scope_analysis_provider import ExternalBase, TODAY
from usermodel.models import User


class BudgetTests(ExternalBase):
    def reply(self, amount=300):
        return ProviderReply(
            None,
            {
                "inputTextTokens": amount - 100,
                "completionTokens": 100,
                "totalTokens": amount,
            },
            "test-model-v1",
        )

    def test_global_budget_is_reserved_before_network_and_no_dispatch_refunds_zero(
        self,
    ):
        run = self.start()
        claim = claim_run(run.pk)
        charge = AnalysisCharge.objects.get()
        self.assertEqual(charge.reserved_microrub, 27852800)
        self.assertEqual(money.used_amount(AnalysisCharge.objects.all()), 27852800)
        self.assertIsNone(charge.generation_dispatched_at)
        finish_run(run.pk, claim.token, error="configuration_missing")
        charge.refresh_from_db()
        self.assertEqual(charge.state, "settled")
        self.assertEqual(charge.charged_microrub, 0)
        self.assertEqual(money.used_amount(AnalysisCharge.objects.all()), 0)

    def test_dispatch_is_single_and_receipt_is_settled_once_at_captured_price(self):
        run = self.start()
        claim = claim_run(run.pk)
        self.assertTrue(mark_generation(run.pk, claim.token))
        self.assertFalse(mark_generation(run.pk, claim.token))
        self.assertTrue(record_receipt(run.pk, claim.token, self.reply()))
        self.assertFalse(record_receipt(run.pk, claim.token, self.reply(400)))
        finish_run(run.pk, claim.token, error="invalid_response")
        charge = AnalysisCharge.objects.get()
        self.assertEqual(charge.charged_microrub, 240000)
        self.assertEqual(charge.profile, run.provider_profile)

    def test_late_known_receipt_refines_charge_without_rewriting_closed_attempt(self):
        run = self.start()
        claim = claim_run(run.pk)
        self.assertTrue(mark_generation(run.pk, claim.token))
        finish_run(run.pk, claim.token, error="provider_timeout", transient=True)
        attempt = run.attempts.get()
        before = AnalysisAttempt.objects.values().get(pk=attempt.pk)
        self.assertEqual(attempt.charge.state, "uncertain")
        self.assertTrue(record_receipt(run.pk, claim.token, self.reply()))
        self.assertEqual(AnalysisAttempt.objects.values().get(pk=attempt.pk), before)
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")
        self.assertIsNone(run.validated_result)
        self.assertEqual(AnalysisCharge.objects.get().charged_microrub, 240000)

    def test_lost_worker_before_dispatch_releases_reserve_and_old_callback_cannot_send(
        self,
    ):
        run = self.start()
        claim = claim_run(run.pk)
        later = timezone.now() + timedelta(seconds=121)
        with patch("mainapp.services.analysis.timezone.now", return_value=later), patch(
            "mainapp.services.analysis.publish_run", return_value=True
        ):
            reconcile_analysis()
            self.assertFalse(mark_generation(run.pk, claim.token))
        charge = AnalysisCharge.objects.get()
        self.assertEqual(charge.charged_microrub, 0)
        self.assertEqual(run.attempts.get().outcome, "worker_lost")

    def test_lost_worker_after_dispatch_keeps_reserve_and_retry_needs_another(self):
        run = self.start()
        claim = claim_run(run.pk)
        mark_generation(run.pk, claim.token)
        later = timezone.now() + timedelta(seconds=121)
        with patch("mainapp.services.analysis.timezone.now", return_value=later), patch(
            "mainapp.services.analysis.publish_run", return_value=True
        ):
            reconcile_analysis()
            next_claim = claim_run(run.pk)
            self.assertIsNotNone(next_claim)
            self.assertNotEqual(claim.token, next_claim.token)
        self.assertEqual(AnalysisCharge.objects.count(), 2)
        self.assertEqual(
            money.used_amount(AnalysisCharge.objects.all()), RESERVE_MICRORUB * 2
        )
        self.assertEqual(
            AnalysisCharge.objects.order_by("created_at").first().state, "uncertain"
        )

    def test_unknown_spend_is_not_reset_by_day_and_fourth_worst_case_is_blocked(self):
        for _ in range(3):
            run = self.start(self.make_request())
            claim = claim_run(run.pk)
            mark_generation(run.pk, claim.token)
            finish_run(run.pk, claim.token, error="provider_timeout")
        self.assertEqual(
            money.used_amount(AnalysisCharge.objects.all()), 3 * RESERVE_MICRORUB
        )
        with patch(
            "mainapp.services.analysis.timezone.now",
            return_value=timezone.now() + timedelta(days=1),
        ):
            fourth = self.start(self.make_request())
            self.assertIsNone(claim_run(fourth.pk))
        fourth.refresh_from_db()
        self.assertEqual(fourth.error_code, "budget_exceeded")
        self.assertEqual(fourth.attempt_count, 0)
        self.assertEqual(ProviderBudget.objects.get().limit_microrub, 100000000)

    def test_revocation_before_dispatch_releases_zero_after_dispatch_keeps_cost(self):
        first = self.start()
        claim = claim_run(first.pk)
        self.membership.role = "viewer"
        self.membership.save()
        self.assertFalse(mark_generation(first.pk, claim.token))
        finish_run(first.pk, claim.token, error="dispatch_denied")
        self.assertEqual(first.attempts.get().charge.charged_microrub, 0)
        self.membership.role = "owner"
        self.membership.save()
        second = self.start()
        claim = claim_run(second.pk)
        mark_generation(second.pk, claim.token)
        self.membership.delete()
        self.assertTrue(record_receipt(second.pk, claim.token, self.reply()))
        finish_run(second.pk, claim.token, error="provider_timeout")
        second.refresh_from_db()
        self.assertEqual(second.error_code, "access_revoked")
        self.assertEqual(second.attempts.get().charge.charged_microrub, 240000)
        self.assertIsNone(second.validated_result)

    def test_bad_usage_suspends_without_refunding_or_negative_charge(self):
        run = self.start()
        claim = claim_run(run.pk)
        mark_generation(run.pk, claim.token)
        self.assertFalse(record_receipt(run.pk, claim.token, self.reply(999999)))
        self.assertTrue(ProviderBudget.objects.get().suspended)
        charge = AnalysisCharge.objects.get()
        self.assertEqual(charge.state, "uncertain")
        self.assertIsNone(charge.charged_microrub)
        self.assertEqual(
            money.used_amount(AnalysisCharge.objects.all()), RESERVE_MICRORUB
        )

    def test_model_and_admin_guards_keep_wallet_global_and_charge_private(self):
        run = self.start()
        claim = claim_run(run.pk)
        mark_generation(run.pk, claim.token)
        record_receipt(run.pk, claim.token, self.reply())
        budget = ProviderBudget.objects.get()
        charge = AnalysisCharge.objects.get()
        for obj in [budget, charge]:
            for operation in [
                lambda: obj.save(),
                lambda: obj.delete(),
                lambda: type(obj).objects.filter(pk=obj.pk).update(id=uuid4()),
                lambda: type(obj)._base_manager.filter(pk=obj.pk).delete(),
                lambda: type(obj).objects.bulk_update([obj], ["created_at"]),
            ]:
                with self.assertRaises(ValidationError):
                    operation()
            request = RequestFactory().get("/admin/")
            request.user = self.owner
            model_admin = admin.site._registry[type(obj)]
            self.assertFalse(model_admin.get_queryset(request).exists())
            self.assertFalse(model_admin.has_view_permission(request, obj))
            for permission in [
                "has_add_permission",
                "has_change_permission",
                "has_delete_permission",
            ]:
                self.assertFalse(getattr(model_admin, permission)(request))
        budget.limit_microrub += 1
        with _domain_write(), self.assertRaises(ValidationError):
            budget.save()
        with _domain_write(), self.assertRaises(ValidationError):
            charge.save()


@override_settings(
    SCOPE_ANALYSIS_BACKEND="yandex",
    SCOPE_YANDEX_FOLDER_ID="synthetic-folder",
    SCOPE_YANDEX_PROCESSING_APPROVED=True,
    SCOPE_YANDEX_PRICE_CONFIRMED_ON=TODAY,
)
@skipUnlessDBFeature("has_select_for_update")
class BudgetConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.actors = []
        for suffix in ["a", "b"]:
            user = User.objects.create_user(email=f"financial-{suffix}@example.com")
            team = Team.objects.create(name=suffix, slug=suffix)
            TeamMembership.objects.create(team=team, user=user, role="owner")
            project = create_project(user, team.pk, name=suffix)
            version = confirm_scope(
                user, team.pk, project.pk, body="форма", confirmed=True
            )
            self.actors.append((user, team, project, version))

    def start(self, actor=0):
        user, team, project, version = self.actors[actor]
        item = create_request(
            user,
            team.pk,
            project.pk,
            version_id=version.pk,
            body="Synthetic",
            submission_key=uuid4(),
        )
        with patch("mainapp.services.analysis.publish_run", return_value=True):
            return start_analysis(
                user,
                team.pk,
                project.pk,
                item.pk,
                start_key=uuid4(),
                external_processing_confirmed=True,
            )

    def race(self, left, right):
        barrier = Barrier(2)

        def call(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return fn()
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, fn) for fn in [left, right]]
            return [f.result(timeout=30) for f in futures]

    def test_first_wallet_creation_is_unique_across_two_companies(self):
        one, two = self.start(0), self.start(1)
        result = self.race(lambda: claim_run(one.pk), lambda: claim_run(two.pk))
        self.assertTrue(all(x is not None for x in result))
        self.assertEqual(ProviderBudget.objects.count(), 1)
        self.assertEqual(
            money.used_amount(AnalysisCharge.objects.all()), RESERVE_MICRORUB * 2
        )

    def test_last_wallet_slot_is_serialized_across_companies(self):
        for _ in range(2):
            run = self.start()
            claim = claim_run(run.pk)
            mark_generation(run.pk, claim.token)
            finish_run(run.pk, claim.token, error="provider_timeout")
        one, two = self.start(0), self.start(1)
        result = self.race(lambda: claim_run(one.pk), lambda: claim_run(two.pk))
        self.assertEqual(result.count(None), 1)
        self.assertEqual(AnalysisCharge.objects.count(), 3)
        self.assertLessEqual(
            money.used_amount(AnalysisCharge.objects.all()), GLOBAL_LIMIT
        )
        self.assertEqual(
            AnalysisRun.objects.filter(error_code="budget_exceeded").count(), 1
        )

    def test_duplicate_receipts_settle_once_and_preserve_attempt(self):
        run = self.start()
        claim = claim_run(run.pk)
        mark_generation(run.pk, claim.token)
        reply = ProviderReply(
            None,
            {"inputTextTokens": 200, "completionTokens": 100, "totalTokens": 300},
            "test-model-v1",
        )
        before = AnalysisAttempt.objects.values().get()
        result = self.race(
            lambda: record_receipt(run.pk, claim.token, reply),
            lambda: record_receipt(run.pk, claim.token, reply),
        )
        self.assertEqual(sorted(result), [False, True])
        self.assertEqual(AnalysisCharge.objects.get().charged_microrub, 240000)
        self.assertEqual(AnalysisAttempt.objects.values().get(), before)

    def test_dispatch_racing_expired_recovery_cannot_start_paid_call(self):
        run = self.start()
        claim = claim_run(run.pk)
        later = timezone.now() + timedelta(seconds=121)
        with patch("mainapp.services.analysis.timezone.now", return_value=later), patch(
            "mainapp.services.analysis.publish_run", return_value=True
        ):
            result = self.race(
                lambda: mark_generation(run.pk, claim.token), reconcile_analysis
            )
        self.assertFalse(result[0])
        self.assertEqual(AnalysisCharge.objects.get().charged_microrub, 0)
