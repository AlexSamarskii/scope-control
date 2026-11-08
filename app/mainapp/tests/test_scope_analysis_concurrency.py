from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from mainapp.models import Team, TeamMembership
from mainapp.models.analysis import ClientRequest, AnalysisRun, AnalysisAttempt
from mainapp.services.scope_control import create_project, confirm_scope
from mainapp.services.analysis import (
    AnalysisConflict,
    AnalysisQuota,
    create_request,
    start_analysis,
    claim_run,
    finish_run,
    reconcile_analysis,
)
from mainapp.services.analysis_backends import fixture_response
from usermodel.models import User


@override_settings(SCOPE_ANALYSIS_BACKEND="fixture")
@skipUnlessDBFeature("has_select_for_update")
class AnalysisConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.user = User.objects.create_user(email="concurrent-analysis@example.com")
        self.team = Team.objects.create(name="Race", slug="race")
        TeamMembership.objects.create(team=self.team, user=self.user, role="owner")
        self.project = create_project(self.user, self.team.pk, name="Race project")
        self.version = confirm_scope(
            self.user, self.team.pk, self.project.pk, body="One page", confirmed=True
        )
        self.item = self.request()

    def request(self, key=None):
        return create_request(
            self.user,
            self.team.pk,
            self.project.pk,
            version_id=self.version.pk,
            body="Synthetic",
            submission_key=key or uuid4(),
        )

    def start(self, item=None, key=None):
        try:
            return start_analysis(
                self.user,
                self.team.pk,
                self.project.pk,
                (item or self.item).pk,
                start_key=key or uuid4(),
            ).pk
        except AnalysisConflict:
            return "conflict"
        except AnalysisQuota:
            return "quota"

    def race(self, left, right):
        barrier = Barrier(2)

        def call(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return fn()
            finally:
                connections["default"].close()

        with patch(
            "mainapp.services.analysis.publish_run", return_value=True
        ), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, fn) for fn in [left, right]]
            return [f.result(timeout=30) for f in futures]

    def test_simultaneous_request_submission_is_one_record(self):
        key = uuid4()
        ids = self.race(lambda: self.request(key).pk, lambda: self.request(key).pk)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ClientRequest.objects.count(), 2)

    def test_same_start_key_has_one_run(self):
        key = uuid4()
        ids = self.race(lambda: self.start(key=key), lambda: self.start(key=key))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_different_start_keys_cannot_create_two_active_runs(self):
        results = self.race(self.start, self.start)
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_two_claims_have_one_attempt(self):
        with patch("mainapp.services.analysis.publish_run", return_value=True):
            run = self.start()
        results = self.race(lambda: claim_run(run), lambda: claim_run(run))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(AnalysisAttempt.objects.count(), 1)

    def test_two_finishes_commit_one_terminal_result(self):
        with patch("mainapp.services.analysis.publish_run", return_value=True):
            run = self.start()
        claim = claim_run(run)
        finish = lambda: finish_run(
            run, claim.token, raw=fixture_response(claim.packet)
        )
        self.assertEqual(sorted(self.race(finish, finish)), [False, True])
        self.assertEqual(AnalysisRun.objects.get(pk=run).status, "validated")
        self.assertEqual(AnalysisAttempt.objects.count(), 1)

    def test_recover_racing_late_finish_is_fenced(self):
        with patch("mainapp.services.analysis.publish_run", return_value=True):
            run = self.start()
        claim = claim_run(run)
        later = timezone.now() + timedelta(seconds=121)
        with patch("mainapp.services.analysis.timezone.now", return_value=later):
            results = self.race(
                lambda: finish_run(
                    run, claim.token, raw=fixture_response(claim.packet)
                ),
                reconcile_analysis,
            )
        self.assertFalse(results[0])
        record = AnalysisRun.objects.get(pk=run)
        self.assertEqual(record.status, "queued")
        self.assertIsNone(record.validated_result)
        self.assertEqual(record.attempts.get().outcome, "worker_lost")

    @override_settings(SCOPE_ANALYSIS_RUNS_PER_HOUR=1)
    def test_last_hourly_slot_is_not_overspent(self):
        other = self.request()
        results = self.race(self.start, lambda: self.start(other))
        self.assertEqual(results.count("quota"), 1)
        self.assertEqual(AnalysisRun.objects.count(), 1)

    @override_settings(SCOPE_ANALYSIS_ATTEMPTS_PER_DAY=1)
    def test_last_daily_attempt_slot_is_not_overspent(self):
        with patch("mainapp.services.analysis.publish_run", return_value=True):
            one = self.start()
            two = self.start(self.request())
        results = self.race(lambda: claim_run(one), lambda: claim_run(two))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(AnalysisAttempt.objects.count(), 1)
        self.assertEqual(
            AnalysisRun.objects.filter(error_code="quota_exceeded").count(), 1
        )
