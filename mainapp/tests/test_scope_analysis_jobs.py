from datetime import timedelta
from io import StringIO
import json, logging, logging.config
from unittest.mock import patch, Mock
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from mainapp.models.analysis import AnalysisRun, AnalysisAttempt
from mainapp.services.analysis import (
    AnalysisQuota,
    build_input,
    claim_run,
    execute_run,
    finish_run,
    input_hash,
    publish_run,
    reconcile_analysis,
)
from mainapp.services.analysis_backends import TransientAnalysisError, fixture_response
from mainapp.tests.test_scope_analysis import AnalysisBase


class JobTests(AnalysisBase):
    def other_manager_request(self):
        from mainapp.models import TeamMembership
        from mainapp.services.scope_control import create_project, confirm_scope
        from mainapp.services.analysis import create_request
        from mainapp.tests.test_scope_control import verified_user

        manager = verified_user("second-manager@example.com")
        TeamMembership.objects.create(team=self.team, user=manager, role="admin")
        project = create_project(manager, self.team.pk, name="Another project")
        version = confirm_scope(
            manager, self.team.pk, project.pk, body="Another scope", confirmed=True
        )
        item = create_request(
            manager,
            self.team.pk,
            project.pk,
            version_id=version.pk,
            body="Other request",
            submission_key=uuid4(),
        )
        return manager, project, item

    def test_duplicate_delivery_and_terminal_redelivery_do_not_invoke_adapter(self):
        run = self.start()
        claim = claim_run(run.pk)
        backend = Mock(return_value=fixture_response(claim.packet))
        execute_run(run.pk, backend=backend)
        backend.assert_not_called()
        self.assertTrue(
            finish_run(run.pk, claim.token, raw=fixture_response(claim.packet))
        )
        execute_run(run.pk, backend=backend)
        backend.assert_not_called()
        self.assertEqual(AnalysisAttempt.objects.count(), 1)
        self.assertFalse(
            finish_run(run.pk, claim.token, raw=fixture_response(claim.packet))
        )

    def test_lost_publish_stays_queued_and_reconciler_publishes_ids_only(self):
        with patch(
            "mainapp.tasks.analysis.run_scope_analysis.apply_async",
            side_effect=RuntimeError("RAW_PRIVATE_RESPONSE"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                run = self.start()
            self.assertEqual(run.status, "queued")
            self.assertFalse(publish_run(run.pk))
        with patch("mainapp.tasks.analysis.run_scope_analysis.apply_async") as send:
            counts = reconcile_analysis()
        self.assertEqual(counts, {"scanned": 1, "recovered": 0, "published": 1})
        args = send.call_args.kwargs
        self.assertEqual(args["args"], [str(run.pk)])
        self.assertTrue(args["ignore_result"])
        self.assertFalse(args["retry"])
        self.assertNotIn(self.item.body, repr(args))
        self.assertNotIn("result", args)

    def test_transient_errors_backoff_and_stop_after_three_attempts(self):
        run = self.start()
        now = timezone.now()
        backend = Mock(side_effect=TransientAnalysisError("RAW_PROVIDER_BODY"))
        for number, offset, backoff in [(1, 0, 10), (2, 11, 60), (3, 72, None)]:
            current = now + timedelta(seconds=offset)
            with patch("mainapp.services.analysis.timezone.now", return_value=current):
                execute_run(run.pk, backend=backend)
            run.refresh_from_db()
            self.assertEqual(run.attempt_count, number)
            if backoff:
                self.assertEqual(run.status, "queued")
                self.assertEqual(
                    run.next_attempt_at, current + timedelta(seconds=backoff)
                )
                with patch(
                    "mainapp.services.analysis.timezone.now",
                    return_value=current + timedelta(seconds=backoff - 1),
                ):
                    self.assertIsNone(claim_run(run.pk))
            else:
                self.assertEqual(run.status, "failed")
        self.assertEqual(backend.call_count, 3)
        execute_run(run.pk, backend=backend)
        self.assertEqual(backend.call_count, 3)
        self.assertEqual(run.error_code, "transient_error")

    def test_invalid_and_unknown_errors_never_retry_or_persist_body(self):
        for backend, code in [
            (lambda _: '{"price":999,"secret":"RAW_PRIVATE"}', "invalid_response"),
            (Mock(side_effect=RuntimeError("RAW_PRIVATE")), "internal_error"),
        ]:
            run = self.start()
            execute_run(run.pk, backend=backend)
            run.refresh_from_db()
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.error_code, code)
            self.assertIsNone(run.validated_result)
            self.assertEqual(run.attempt_count, 1)
            self.assertNotIn(
                "RAW_PRIVATE", repr(run.__dict__) + repr(list(run.attempts.values()))
            )

    def test_expired_lease_recovery_and_fenced_late_finish(self):
        run = self.start()
        first = claim_run(run.pk)
        now = timezone.now()
        run.refresh_from_db()
        self.assertEqual(
            (run.lease_expires_at - run.attempts.get().started_at).total_seconds(), 120
        )
        with patch(
            "mainapp.services.analysis.timezone.now",
            return_value=now + timedelta(seconds=121),
        ):
            self.assertFalse(
                finish_run(run.pk, first.token, raw=fixture_response(first.packet))
            )
            with patch("mainapp.services.analysis.publish_run", return_value=True):
                counts = reconcile_analysis()
            self.assertEqual(counts["recovered"], 1)
            second = claim_run(run.pk)
            self.assertNotEqual(first.token, second.token)
            self.assertFalse(
                finish_run(run.pk, first.token, raw=fixture_response(first.packet))
            )
            self.assertTrue(
                finish_run(run.pk, second.token, raw=fixture_response(second.packet))
            )
        self.assertEqual(
            list(run.attempts.values_list("outcome", flat=True)),
            ["worker_lost", "validated"],
        )

    def test_three_worker_losses_exhaust_run(self):
        run = self.start()
        now = timezone.now()
        for number in range(3):
            with patch(
                "mainapp.services.analysis.timezone.now",
                return_value=now + timedelta(seconds=number * 122),
            ):
                claim = claim_run(run.pk)
                self.assertIsNotNone(claim)
            with patch(
                "mainapp.services.analysis.timezone.now",
                return_value=now + timedelta(seconds=number * 122 + 121),
            ), patch("mainapp.services.analysis.publish_run", return_value=True):
                reconcile_analysis()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "attempts_exhausted")
        self.assertEqual(run.attempt_count, 3)
        self.assertEqual(run.attempts.count(), 3)

    def test_hourly_limit_aggregates_projects_and_duplicate_key_is_free(self):
        with override_settings(SCOPE_ANALYSIS_RUNS_PER_HOUR=2):
            run = self.start()
            execute_run(run.pk)
            self.assertEqual(self.start(start_key=run.start_key).pk, run.pk)
            from mainapp.services.analysis import start_analysis

            manager, project, item = self.other_manager_request()
            second = start_analysis(
                manager, self.team.pk, project.pk, item.pk, start_key=uuid4()
            )
            execute_run(second.pk)
            with self.assertRaises(AnalysisQuota):
                self.start(self.make_request())
            self.client.force_login(self.owner)
            response = self.client.post(self.analyze_url(), {"start_key": uuid4()})
            self.assertEqual(response.status_code, 429)
            later = timezone.now() + timedelta(hours=1, seconds=1)
            with patch("mainapp.services.analysis.timezone.now", return_value=later):
                self.assertEqual(self.start().status, "queued")

    def test_daily_attempt_quota_no_adapter_call_and_utc_reset(self):
        with override_settings(SCOPE_ANALYSIS_ATTEMPTS_PER_DAY=1):
            run = self.start()
            execute_run(run.pk)
            from mainapp.services.analysis import start_analysis

            manager, project, item = self.other_manager_request()
            second = start_analysis(
                manager, self.team.pk, project.pk, item.pk, start_key=uuid4()
            )
            adapter = Mock(return_value=fixture_response({}))
            execute_run(second.pk, backend=adapter)
            adapter.assert_not_called()
            second.refresh_from_db()
            self.assertEqual(second.error_code, "quota_exceeded")
            self.assertEqual(second.attempt_count, 0)
            later = timezone.now() + timedelta(days=1)
            with patch("mainapp.services.analysis.timezone.now", return_value=later):
                execute_run(second.pk, backend=adapter)
                adapter.assert_not_called()
                third = self.start()
                execute_run(third.pk, backend=adapter)
            adapter.assert_called_once()

    def test_disabled_unknown_backend_cannot_start_and_no_network_in_fixture(self):
        for value in ["disabled", "openai", "arbitrary.module"]:
            with override_settings(SCOPE_ANALYSIS_BACKEND=value), self.assertRaises(
                ValidationError
            ):
                self.start()
        self.assertFalse(AnalysisRun.objects.exists())
        run = self.start()
        with patch(
            "socket.create_connection", side_effect=AssertionError("network")
        ), patch(
            "requests.sessions.Session.request", side_effect=AssertionError("http")
        ), patch(
            "httpx.Client.send", side_effect=AssertionError("httpx")
        ):
            execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "validated")

    def test_disabled_between_enqueue_and_claim_never_calls_adapter(self):
        run = self.start()
        adapter = Mock()
        with override_settings(SCOPE_ANALYSIS_BACKEND="disabled"):
            execute_run(run.pk, backend=adapter)
        adapter.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.error_code, "backend_disabled")

    def test_synthetic_hash_keyed_response_and_escaped_result(self):
        run = self.start()
        expected = run.input_sha256

        def backend(packet):
            self.assertEqual(input_hash(packet), expected)
            return json.dumps(
                {
                    "schema_version": 1,
                    "category": "included",
                    "rationale": "<script>evil</script>",
                    "citations": [
                        dict(
                            source_id=f"scope:{self.version.pk}:body",
                            start=0,
                            end=len(self.version.body),
                            quote=self.version.body,
                        )
                    ],
                    "questions": [],
                }
            )

        execute_run(run.pk, backend=backend)
        self.client.force_login(self.owner)
        response = self.client.get(self.detail_url())
        self.assertContains(response, "Входит в объём")
        self.assertContains(response, "&lt;script&gt;evil&lt;/script&gt;")
        self.assertNotContains(response, "<script>evil</script>")

    def test_reconciler_command_and_cap(self):
        with patch(
            "mainapp.services.analysis.publish_run", return_value=True
        ) as publish:
            self.start()
            out = StringIO()
            call_command("reconcile_scope_analysis", stdout=out)
            self.assertIn("published=1", out.getvalue())
            publish.assert_called_once()
        with patch("mainapp.services.analysis.publish_run", return_value=True):
            self.assertEqual(reconcile_analysis(limit=1000)["scanned"], 1)

    def test_web_and_worker_logs_do_not_capture_private_markers(self):
        from project.celeryapp import receiver_setup_logging

        marker = "S2A_RAW_PRIVATE_LOG_MARKER"
        for setup in [
            lambda: logging.config.dictConfig(settings.LOGGING),
            lambda: receiver_setup_logging(
                loglevel=logging.WARNING, logfile=None, format=None, colorize=False
            ),
        ]:
            setup()
            stream = StringIO()
            handler = logging.StreamHandler(stream)
            root = logging.getLogger()
            root.addHandler(handler)
            # Also capture the configured console handlers, including non-propagating SQL.
            console_streams = []
            seen = set()
            for name in ["", "django.db.backends", "django.server"]:
                for current in logging.getLogger(name).handlers:
                    if (
                        current is not handler
                        and id(current) not in seen
                        and hasattr(current, "setStream")
                    ):
                        seen.add(id(current))
                        console_streams.append((current, current.stream))
                        current.setStream(stream)
            try:
                item = self.make_request(source_description=marker)
                run = self.start(item)
                with patch(
                    "mainapp.tasks.analysis.run_scope_analysis.apply_async",
                    side_effect=RuntimeError(marker),
                ):
                    publish_run(run.pk)
                execute_run(run.pk, backend=Mock(side_effect=RuntimeError(marker)))
                self.client.force_login(self.owner)
                self.client.get(self.detail_url(item))
                logging.getLogger("django.db.backends").debug("%s", marker)
                logging.getLogger("django.server").warning(
                    "GET /teams/invitations/SECRET_AUTH_MARKER/accept/"
                )
            finally:
                root.removeHandler(handler)
                for current, old in console_streams:
                    current.setStream(old)
            self.assertNotIn(marker, stream.getvalue())
            self.assertNotIn("SECRET_AUTH_MARKER", stream.getvalue())

    def test_reconciler_scans_oldest_hundred_only(self):
        with override_settings(SCOPE_ANALYSIS_RUNS_PER_HOUR=200):
            expected = [str(self.start(self.make_request()).pk) for _ in range(101)]
        with patch(
            "mainapp.services.analysis.publish_run", return_value=True
        ) as publish:
            result = reconcile_analysis(limit=1000)
        self.assertEqual(result["scanned"], 100)
        self.assertEqual(
            [call.args[0] for call in publish.call_args_list], expected[:100]
        )

    def test_task_boundary_suppresses_unexpected_exception_contents(self):
        from mainapp.tasks.analysis import run_scope_analysis

        with patch(
            "mainapp.tasks.analysis.execute_run",
            side_effect=RuntimeError("PRIVATE_DATABASE_ERROR"),
        ):
            with self.assertLogs("mainapp.tasks.analysis", level="WARNING") as logs:
                self.assertIsNone(run_scope_analysis(str(uuid4())))
        self.assertNotIn("PRIVATE_DATABASE_ERROR", str(logs.output))

    def test_real_refused_broker_has_no_internal_backoff(self):
        import socket
        import time
        from kombu import Connection

        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        connection = Connection(
            f"redis://127.0.0.1:{blocker.getsockname()[1]}/0",
            connect_timeout=3,
            transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
        )
        try:
            with patch(
                "mainapp.tasks.analysis.run_scope_analysis.app.connection_for_write",
                return_value=connection,
            ):
                started = time.monotonic()
                self.assertFalse(publish_run(uuid4()))
                self.assertLess(time.monotonic() - started, 3.5)
        finally:
            connection.release()
            blocker.close()
