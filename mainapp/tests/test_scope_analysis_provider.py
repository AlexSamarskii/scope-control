from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
import logging
import os
from pathlib import Path
import socket
from threading import Thread
import time
from unittest.mock import patch, Mock
from uuid import uuid4

import httpx
from django.db import transaction
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from mainapp.models.analysis import AnalysisRun
from mainapp.models.analysis_budget import AnalysisCharge, ProviderBudget
from mainapp.services.analysis import (
    build_input,
    claim_run,
    execute_run,
    finish_run,
    mark_generation,
    record_receipt,
    start_analysis,
)
from mainapp.services.analysis_provider import (
    ProviderFailure,
    analyze_yandex,
    checked_usage,
    completion_payload,
    normalize_response,
    public_profile,
    RESERVE_MICRORUB,
)
from mainapp.services.analysis_validation import InvalidAnalysis
from mainapp.services.analysis_yandex_http import (
    HTTPReply,
    perform_request,
    request_http,
)
from mainapp.tests.test_scope_analysis import AnalysisBase

TODAY = timezone.now().date().isoformat()
CASES = json.loads(
    (Path(__file__).parent / "fixtures/yandex_analysis_responses.json").read_text()
)


def success_envelope(packet):
    result = deepcopy(CASES["result"])
    result["citations"][0]["source_id"] = next(
        x for x in packet["sources"] if x.startswith("scope:") and x.endswith(":body")
    )
    return {
        "alternatives": [
            {
                "status": "ALTERNATIVE_STATUS_FINAL",
                "message": {
                    "role": "assistant",
                    "text": json.dumps(result, ensure_ascii=False),
                },
            }
        ],
        "usage": deepcopy(CASES["usage"]),
        "modelVersion": CASES["modelVersion"],
    }


def protocol_double(
    packet, *, envelope=None, tokenizer=None, generation_error=None, before_call=None
):
    """Explicit offline protocol double; never used by public registry or live CLI."""
    envelope = success_envelope(packet) if envelope is None else envelope
    tokenizer = (
        {
            "tokens": [{"id": "1", "text": "private", "special": False}],
            "modelVersion": CASES["modelVersion"],
        }
        if tokenizer is None
        else tokenizer
    )

    def send(path, payload):
        if before_call:
            before_call(path, payload)
        if path == "tokenizeCompletion":
            return HTTPReply(200, json.dumps(tokenizer).encode())
        if generation_error is not None:
            return generation_error
        return HTTPReply(200, json.dumps(envelope, ensure_ascii=False).encode())

    return send


@override_settings(
    SCOPE_ANALYSIS_BACKEND="yandex",
    SCOPE_YANDEX_FOLDER_ID="synthetic-folder",
    SCOPE_YANDEX_PROCESSING_APPROVED=True,
    SCOPE_YANDEX_PRICE_CONFIRMED_ON=TODAY,
)
class ExternalBase(AnalysisBase):
    def start(self, item=None, **kwargs):
        return start_analysis(
            self.owner,
            self.team.pk,
            self.project.pk,
            (item or self.item).pk,
            start_key=kwargs.get("start_key", uuid4()),
            external_processing_confirmed=kwargs.get("consent", True),
        )

    def packet(self, run=None):
        return build_input(run or self.start())


class ProviderTests(ExternalBase):
    def test_profile_captured_no_key_and_consent_required(self):
        with self.assertRaises(ValidationError):
            self.start(consent=False)
        self.assertFalse(AnalysisRun.objects.exists())
        run = self.start()
        self.assertEqual(run.backend, "yandex")
        self.assertIsNotNone(run.external_processing_confirmed_at)
        self.assertEqual(run.provider_profile, public_profile())
        self.assertNotIn("API_KEY", json.dumps(run.provider_profile))
        self.assertTrue(run.provider_profile["model_uri"].endswith("/yandexgpt-5.1"))

    def test_invalid_folder_permission_and_stale_price_refuse_before_run(self):
        for values in [
            {"SCOPE_YANDEX_FOLDER_ID": "https://attacker.invalid"},
            {"SCOPE_YANDEX_PROCESSING_APPROVED": False},
            {"SCOPE_YANDEX_PRICE_CONFIRMED_ON": "invalid"},
            {
                "SCOPE_YANDEX_PRICE_CONFIRMED_ON": (
                    timezone.now().date() - timedelta(days=7)
                ).isoformat()
            },
            {
                "SCOPE_YANDEX_PRICE_CONFIRMED_ON": (
                    timezone.now().date() + timedelta(days=1)
                ).isoformat()
            },
        ]:
            with override_settings(**values), self.assertRaises(ValidationError):
                self.start()
        self.assertFalse(AnalysisRun.objects.exists())

    def test_protocol_payload_has_schema_full_input_and_no_metadata_url(self):
        packet = self.packet()
        payload = completion_payload(packet)
        self.assertEqual(
            set(payload), {"modelUri", "completionOptions", "messages", "jsonSchema"}
        )
        self.assertEqual(
            payload["completionOptions"],
            {
                "stream": False,
                "temperature": 0,
                "maxTokens": "2048",
                "reasoningOptions": {"mode": "DISABLED"},
            },
        )
        sent = json.loads(payload["messages"][1]["text"])
        self.assertEqual(sent["sources"], packet["sources"])
        self.assertNotIn("source_description", sent)
        ids = payload["jsonSchema"]["schema"]["properties"]["citations"]["items"][
            "properties"
        ]["source_id"]["enum"]
        self.assertEqual(ids, list(packet["sources"]))

    def test_real_protocol_flow_is_validated_and_charged_with_offline_transport(self):
        run = self.start()
        packet = build_input(run)
        calls = []

        def before(path, payload):
            calls.append(path)
            self.assertEqual(payload, completion_payload(packet))
            self.assertEqual(AnalysisCharge.objects.count(), 1)
            if path == "completion":
                self.assertIsNotNone(
                    AnalysisCharge.objects.get().generation_dispatched_at
                )

        with patch(
            "mainapp.services.analysis_provider.request_http",
            side_effect=protocol_double(packet, before_call=before),
        ):
            execute_run(run.pk)
        run.refresh_from_db()
        charge = AnalysisCharge.objects.get()
        self.assertEqual(run.status, "validated")
        self.assertEqual(run.validated_result["category"], "included")
        self.assertEqual(calls, ["tokenizeCompletion", "completion"])
        self.assertEqual(charge.state, "settled")
        self.assertEqual(charge.charged_microrub, 240000)
        self.assertEqual(charge.model_version, CASES["modelVersion"])
        cite = run.validated_result["citations"][0]
        self.assertEqual(
            packet["sources"][cite["source_id"]][cite["start"] : cite["end"]], "форма"
        )

    def test_official_result_wrapper_is_validated_and_settled(self):
        run = self.start()
        packet = build_input(run)
        with patch(
            "mainapp.services.analysis_provider.request_http",
            side_effect=protocol_double(
                packet, envelope={"result": success_envelope(packet)}
            ),
        ):
            execute_run(run.pk)
        run.refresh_from_db()
        charge = AnalysisCharge.objects.get(attempt__run=run)
        self.assertEqual(run.status, "validated")
        self.assertEqual(run.validated_result["category"], "included")
        self.assertEqual(charge.state, "settled")
        self.assertEqual(charge.charged_microrub, 240000)

    def test_mixed_or_invalid_result_wrapper_is_rejected(self):
        packet = self.packet()
        for envelope in (
            {"result": None},
            {"result": []},
            {"result": success_envelope(packet), **success_envelope(packet)},
            {"result": {"result": success_envelope(packet)}},
        ):
            with self.subTest(envelope_shape=list(envelope)), patch(
                "mainapp.services.analysis_provider.request_http",
                side_effect=protocol_double(packet, envelope=envelope),
            ):
                try:
                    reply = analyze_yandex(packet, lambda: True)
                except ProviderFailure as exc:
                    self.assertEqual(exc.code, "provider_protocol")
                else:
                    self.assertIsNone(reply.raw)
                    self.assertEqual(reply.error, "provider_usage_anomaly")

    def test_provider_quote_normalization_rejects_ambiguity_and_foreign_source(self):
        packet = self.packet()
        valid = success_envelope(packet)
        text = valid["alternatives"][0]["message"]["text"]
        result = json.loads(text)
        source_id = result["citations"][0]["source_id"]
        sources = {source_id: "😀 форма"}
        raw = normalize_response(text, sources)
        self.assertEqual(json.loads(raw)["citations"][0]["start"], 2)
        for source in ["нет цитаты", "форма форма", "формА", "фо рма"]:
            with self.assertRaises(InvalidAnalysis):
                normalize_response(text, {source_id: source})
        result["citations"][0].update(source_id="scope:foreign:body")
        with self.assertRaises(InvalidAnalysis):
            normalize_response(json.dumps(result), packet["sources"])
        result = json.loads(text)
        result["citations"][0].update(start=0)
        with self.assertRaises(InvalidAnalysis):
            normalize_response(json.dumps(result), packet["sources"])
        overlap = {
            **json.loads(text),
            "citations": [{"source_id": source_id, "quote": "аа"}],
        }
        with self.assertRaises(InvalidAnalysis):
            normalize_response(json.dumps(overlap), {source_id: "ааа"})

    def test_missing_wrong_or_oversize_provider_result_is_not_fixed(self):
        packet = self.packet()
        good = json.loads(
            success_envelope(packet)["alternatives"][0]["message"]["text"]
        )
        for change in [
            {"price": 100},
            {"schema_version": True},
            {"citations": {}},
            {"category": "approved"},
            {"questions": ["x" * 501]},
            {"rationale": "x" * 2001},
        ]:
            with self.assertRaises(InvalidAnalysis):
                normalize_response(json.dumps({**good, **change}), packet["sources"])
        for raw in ['{"x":1,"x":2}', '{"x":NaN}', "[" * 2000 + "]" * 2000, "x" * 32769]:
            with self.assertRaises(InvalidAnalysis):
                normalize_response(raw, packet["sources"])

    def test_tokenizer_too_large_or_failed_never_dispatches_generation(self):
        run = self.start()
        packet = build_input(run)
        tokens = {"tokens": [{}] * 30721, "modelVersion": CASES["modelVersion"]}
        with patch(
            "mainapp.services.analysis_provider.request_http",
            side_effect=protocol_double(packet, tokenizer=tokens),
        ) as send:
            execute_run(run.pk)
        self.assertEqual(send.call_count, 1)
        run.refresh_from_db()
        self.assertEqual(run.error_code, "input_too_large")
        charge = AnalysisCharge.objects.get()
        self.assertEqual(charge.charged_microrub, 0)
        self.assertIsNone(charge.generation_dispatched_at)

    def test_generation_failure_mapping_and_cost_reserve(self):
        cases = [
            (HTTPReply(401), "provider_auth", False),
            (HTTPReply(403), "provider_auth", False),
            (HTTPReply(302), "provider_rejected", False),
            (HTTPReply(429), "provider_rate_limit", True),
            (HTTPReply(503), "provider_unavailable", True),
            (HTTPReply(error="provider_timeout"), "provider_timeout", True),
            (HTTPReply(200, b"malformed"), "provider_protocol", False),
        ]
        for reply, code, queued in cases:
            with self.subTest(code=code), transaction.atomic():
                run = self.start(self.make_request())
                packet = build_input(run)
                with patch(
                    "mainapp.services.analysis_provider.request_http",
                    side_effect=protocol_double(packet, generation_error=reply),
                ):
                    execute_run(run.pk)
                run.refresh_from_db()
                self.assertEqual(run.error_code, code)
                self.assertEqual(run.status, "queued" if queued else "failed")
                charge = run.attempts.get().charge
                self.assertEqual(charge.state, "uncertain")
                self.assertEqual(charge.reserved_microrub, RESERVE_MICRORUB)
                transaction.set_rollback(True)

    def test_refusal_truncation_tool_output_and_invalid_quotes_are_not_results(self):
        # A single successful usage can be settled even when the answer is invalid.
        for kind in ["filter", "truncate", "tools", "quote"]:
            run = self.start(self.make_request())
            packet = build_input(run)
            envelope = success_envelope(packet)
            alt = envelope["alternatives"][0]
            if kind == "filter":
                alt["status"] = CASES["filter_status"]
            if kind == "truncate":
                alt["status"] = CASES["truncated_status"]
            if kind == "tools":
                alt["message"] = {"role": "assistant", "toolCallList": {}}
            if kind == "quote":
                alt["message"]["text"] = alt["message"]["text"].replace(
                    "форма", "выдуманная цитата"
                )
            with patch(
                "mainapp.services.analysis_provider.request_http",
                side_effect=protocol_double(packet, envelope=envelope),
            ):
                execute_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, "failed")
            self.assertIsNone(run.validated_result)
            self.assertEqual(run.attempts.get().charge.charged_microrub, 240000)

    def test_bad_usage_or_version_suspends_wallet(self):
        run = self.start()
        packet = build_input(run)
        envelope = success_envelope(packet)
        envelope["usage"]["totalTokens"] = "999999999"
        with patch(
            "mainapp.services.analysis_provider.request_http",
            side_effect=protocol_double(packet, envelope=envelope),
        ):
            execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.error_code, "provider_usage_anomaly")
        self.assertTrue(ProviderBudget.objects.get().suspended)
        another = self.start(self.make_request())
        execute_run(another.pk)
        another.refresh_from_db()
        self.assertEqual(another.error_code, "budget_exceeded")
        self.assertEqual(another.attempt_count, 0)

    def test_consent_roles_and_template_distinguish_fixture_and_real(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(self.analyze_url(), {"start_key": uuid4()}).status_code,
            400,
        )
        self.assertFalse(AnalysisRun.objects.exists())
        response = self.client.get(self.detail_url())
        self.assertContains(response, "YandexGPT Pro 5.1")
        self.assertContains(response, "синтетические")
        self.assertNotContains(response, "Тестовый анализ: модель не подключена.")
        self.membership.role = "viewer"
        self.membership.save()
        self.assertEqual(
            self.client.post(
                self.analyze_url(),
                {"start_key": uuid4(), "external_processing_confirmed": "on"},
            ).status_code,
            403,
        )

    def test_configuration_change_before_claim_never_calls_provider(self):
        run = self.start()
        with override_settings(SCOPE_YANDEX_FOLDER_ID="different-folder"), patch(
            "mainapp.services.analysis_provider.request_http"
        ) as send:
            execute_run(run.pk)
            send.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.error_code, "configuration_changed")
        self.assertEqual(run.attempt_count, 0)

    def test_key_missing_child_refuses_without_network_and_releases_zero(self):
        run = self.start()
        with patch.dict(os.environ, {"SCOPE_YANDEX_API_KEY": ""}):
            execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.error_code, "configuration_missing")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.attempts.get().charge.charged_microrub, 0)

    def test_smoke_command_refuses_existing_project_text_without_sending(self):
        with patch(
            "mainapp.services.analysis_provider.request_http"
        ) as send, self.assertRaises(CommandError):
            call_command(
                "run_model_smoke",
                team_id=str(self.team.pk),
                project_id=str(self.project.pk),
                user_id=str(self.owner.pk),
                stdout=StringIO(),
            )
        send.assert_not_called()
        self.assertFalse(AnalysisRun.objects.exists())

    def test_version_change_suspends_and_known_usage_remains_accounted(self):
        run = self.start()
        packet = build_input(run)
        envelope = success_envelope(packet)
        envelope["modelVersion"] = "different-model-version"
        with patch(
            "mainapp.services.analysis_provider.request_http",
            side_effect=protocol_double(packet, envelope=envelope),
        ):
            execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertTrue(ProviderBudget.objects.get().suspended)
        self.assertEqual(run.attempts.get().charge.charged_microrub, 240000)
        self.assertIsNone(run.validated_result)

    def test_foreign_actor_cannot_consent_or_trigger_external_processing(self):
        from django.http import Http404

        self.client.force_login(self.outsider)
        with patch("mainapp.services.analysis_provider.request_http") as send:
            self.assertEqual(
                self.client.post(
                    self.analyze_url(),
                    {"start_key": uuid4(), "external_processing_confirmed": "on"},
                ).status_code,
                404,
            )
            with self.assertRaises(Http404):
                start_analysis(
                    self.outsider,
                    self.other_team.pk,
                    self.other_project.pk,
                    self.item.pk,
                    start_key=uuid4(),
                    external_processing_confirmed=True,
                )
            send.assert_not_called()
        self.assertFalse(AnalysisRun.objects.exists())

    def test_private_source_response_key_and_exception_markers_do_not_enter_logs(self):
        import logging.config
        from django.conf import settings
        from mainapp.services.analysis import create_request
        from project.celeryapp import receiver_setup_logging

        marker = "S2B_PRIVATE_INPUT_OUTPUT_KEY_MARKER"
        for setup in [
            lambda: logging.config.dictConfig(settings.LOGGING),
            lambda: receiver_setup_logging(
                loglevel=logging.DEBUG, logfile=None, format=None, colorize=False
            ),
        ]:
            setup()
            records = []

            class Capture(logging.Handler):
                def emit(self, record):
                    records.append(record.getMessage())

            handler = Capture()
            root = logging.getLogger()
            previous = root.level
            root.setLevel(logging.DEBUG)
            root.addHandler(handler)
            try:
                item = create_request(
                    self.owner,
                    self.team.pk,
                    self.project.pk,
                    version_id=self.version.pk,
                    body=marker,
                    source_description=marker,
                    submission_key=uuid4(),
                )
                run = self.start(item)
                packet = build_input(run)
                envelope = success_envelope(packet)
                result = json.loads(envelope["alternatives"][0]["message"]["text"])
                result["rationale"] = marker
                envelope["alternatives"][0]["message"]["text"] = json.dumps(result)
                with patch(
                    "mainapp.services.analysis_provider.request_http",
                    side_effect=protocol_double(packet, envelope=envelope),
                ):
                    execute_run(run.pk)
                # Third-party verbose messages must be suppressed by production configuration.
                logging.getLogger("httpcore").debug(marker)
                logging.getLogger("httpx").debug(marker)
                process = Mock()
                process.communicate.return_value = (marker.encode(), marker.encode())
                process.returncode = 0
                process.poll.return_value = 0
                with patch(
                    "mainapp.services.analysis_yandex_http.subprocess.Popen",
                    return_value=process,
                ):
                    self.assertEqual(
                        request_http("completion", completion_payload(packet)).error,
                        "provider_transport",
                    )
            finally:
                root.removeHandler(handler)
                root.setLevel(previous)
            self.assertNotIn(marker, "\n".join(records))


class HTTPBoundaryTests(SimpleTestCase):
    def payload(self):
        return {"modelUri": "gpt://synthetic-folder/yandexgpt-5.1", "messages": []}

    def test_fixed_protocol_headers_no_redirect_retry_and_bounded_response(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://attacker.invalid"})

        reply = perform_request(
            "completion",
            self.payload(),
            "SYNTHETIC_KEY",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(reply.status, 302)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(
            str(request.url),
            "https://ai.api.cloud.yandex.net/foundationModels/v1/completion",
        )
        self.assertEqual(request.headers["authorization"], "Api-Key SYNTHETIC_KEY")
        self.assertEqual(request.headers["x-data-logging-enabled"], "false")
        self.assertEqual(request.headers["x-folder-id"], "synthetic-folder")
        huge = httpx.MockTransport(
            lambda r: httpx.Response(200, stream=httpx.ByteStream(b"x" * 262145))
        )
        self.assertEqual(
            perform_request("completion", self.payload(), "fake", transport=huge).error,
            "response_too_large",
        )
        zipped = httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=httpx.ByteStream(b"bad"),
            )
        )
        self.assertEqual(
            perform_request(
                "completion", self.payload(), "fake", transport=zipped
            ).error,
            "provider_transport",
        )

    def test_local_proxy_is_ignored_and_header_and_trickle_deadlines_interrupt_io(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            mode = "fast"

            def log_message(self, *args):
                pass

            def do_POST(self):
                # Consume the request before closing the HTTP/1.0 connection;
                # otherwise unread POST bytes can produce an intermittent TCP RST.
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.mode == "headers":
                    time.sleep(1)
                try:
                    self.send_response(200)
                    self.end_headers()
                    if self.mode == "trickle":
                        for _ in range(20):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.1)
                    else:
                        self.wfile.write(b"{}")
                except OSError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "ALL_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                },
            ):
                for _ in range(5):
                    response = perform_request(
                        "completion", self.payload(), "fake", base_url=base
                    )
                    self.assertEqual(response.status, 200, response)
                    self.assertEqual(response.error, "", response)
                    self.assertEqual(response.body, b"{}")
                for mode in ["headers", "trickle"]:
                    Handler.mode = mode
                    started = time.monotonic()
                    reply = perform_request(
                        "completion",
                        self.payload(),
                        "fake",
                        base_url=base,
                        wall_seconds=0.2,
                    )
                    self.assertEqual(reply.error, "provider_timeout")
                    self.assertLess(time.monotonic() - started, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_parent_deadline_kills_and_reaps_actual_child(self):
        import subprocess, sys

        real_popen = subprocess.Popen
        children = []

        def sleeper(*args, **kwargs):
            child = real_popen(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            children.append(child)
            return child

        with patch(
            "mainapp.services.analysis_yandex_http.subprocess.Popen",
            side_effect=sleeper,
        ), patch("mainapp.services.analysis_yandex_http.WALL_SECONDS", 0.2):
            started = time.monotonic()
            reply = request_http("completion", self.payload())
        self.assertEqual(reply.error, "provider_timeout")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_http_failure_does_not_return_private_error_body(self):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(503, content=b"PRIVATE_PROVIDER_ERROR_BODY")
        )
        reply = perform_request(
            "completion", self.payload(), "fake", transport=transport
        )
        self.assertEqual(reply.status, 503)
        self.assertEqual(reply.body, b"")
        self.assertNotIn("PRIVATE", repr(reply))


class UsageContractTests(SimpleTestCase):
    def test_strict_counters_and_hidden_reasoning_anomalies_are_rejected(self):
        self.assertEqual(
            checked_usage(CASES["usage"]),
            {"inputTextTokens": 200, "completionTokens": 100, "totalTokens": 300},
        )
        for value in [True, 200, "-1", "+1", "01", "1.5", "9999999999999999", None]:
            with self.assertRaises(ProviderFailure):
                checked_usage({**CASES["usage"], "inputTextTokens": value})
        for usage in [
            {**CASES["usage"], "totalTokens": "1"},
            {**CASES["usage"], "completionTokensDetails": {"reasoningTokens": "5"}},
            {},
        ]:
            with self.assertRaises(ProviderFailure):
                checked_usage(usage)
