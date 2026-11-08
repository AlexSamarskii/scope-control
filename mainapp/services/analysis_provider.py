"""Native Yandex protocol, immutable public profile, and exact quote mapping."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from mainapp.services.analysis_validation import (
    InvalidAnalysis,
    parse_object,
    validate_response,
)
from mainapp.services.analysis_yandex_http import request_http

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
PRICE_REVISION = "yc-yandexgpt51-sync-rub-20261106"
RESERVE_MICRORUB = (32768 + 2048) * 800


class ProviderFailure(Exception):
    def __init__(self, code, *, transient=False):
        self.code = code
        self.transient = transient
        super().__init__(code)


@dataclass(frozen=True)
class ProviderReply:
    raw: str | None
    usage: dict | None
    model_version: str
    error: str = ""
    anomaly: bool = False


def public_profile():
    folder = settings.SCOPE_YANDEX_FOLDER_ID
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{5,128}", folder)
        or not settings.SCOPE_YANDEX_PROCESSING_APPROVED
    ):
        raise ValidationError(
            "Настройте RU-каталог и разрешение синтетической внешней обработки."
        )
    try:
        confirmed = date.fromisoformat(settings.SCOPE_YANDEX_PRICE_CONFIRMED_ON)
    except (ValueError, TypeError):
        raise ValidationError("Подтвердите актуальный тариф YandexGPT.") from None
    if not timedelta(0) <= timezone.now().date() - confirmed < timedelta(days=7):
        raise ValidationError("Подтверждение тарифа устарело или имеет неверную дату.")
    return {
        "id": "yandex-ru-synthetic-v1",
        "model_uri": f"gpt://{folder}/yandexgpt-5.1",
        "endpoint": "https://ai.api.cloud.yandex.net",
        "prompt_revision": "scope-analysis-v1",
        "prompt_sha256": sha256(
            (PROMPTS / "scope_analysis_v1.txt").read_bytes()
        ).hexdigest(),
        "schema_revision": "yandex-analysis-v1",
        "schema_sha256": sha256(
            (PROMPTS / "yandex_analysis_v1.json").read_bytes()
        ).hexdigest(),
        "normalizer_revision": "unique-quote-v1",
        "price_revision": PRICE_REVISION,
        "price_confirmed_on": confirmed.isoformat(),
        "input_ceiling": 32768,
        "max_input_tokens": 30720,
        "max_output_tokens": 2048,
        "microrub_per_token": 800,
        "data_class": "synthetic_only",
    }


def check_profile(profile):
    try:
        current = public_profile()
    except ValidationError:
        raise ProviderFailure("configuration_changed") from None
    if type(profile) is not dict or profile != current:
        raise ProviderFailure("configuration_changed")


def completion_payload(packet):
    profile = packet["provider_profile"]
    check_profile(profile)
    schema = json.loads((PROMPTS / "yandex_analysis_v1.json").read_text())
    schema["properties"]["citations"]["items"]["properties"]["source_id"]["enum"] = (
        list(packet["sources"])
    )
    # Source descriptions/URLs and keys are deliberately absent.
    user_data = {
        key: packet[key] for key in ("request_id", "scope_version_id", "sources")
    }
    return {
        "modelUri": profile["model_uri"],
        "completionOptions": {
            "stream": False,
            "temperature": 0,
            "maxTokens": "2048",
            "reasoningOptions": {"mode": "DISABLED"},
        },
        "messages": [
            {"role": "system", "text": (PROMPTS / "scope_analysis_v1.txt").read_text()},
            {
                "role": "user",
                "text": json.dumps(user_data, ensure_ascii=False, sort_keys=True),
            },
        ],
        "jsonSchema": {"schema": schema},
    }


def normalize_response(raw, sources):
    result = parse_object(raw)
    if set(result) != {
        "schema_version",
        "category",
        "rationale",
        "citations",
        "questions",
    }:
        raise InvalidAnalysis()
    if type(result["citations"]) is not list or len(result["citations"]) > 8:
        raise InvalidAnalysis()
    normalized = deepcopy(result)
    for citation in normalized["citations"]:
        if type(citation) is not dict or set(citation) != {"source_id", "quote"}:
            raise InvalidAnalysis()
        source_id, quote = citation["source_id"], citation["quote"]
        if (
            type(source_id) is not str
            or source_id not in sources
            or type(quote) is not str
            or not quote.strip()
            or len(quote) > 2000
        ):
            raise InvalidAnalysis()
        source = sources[source_id]
        start = source.find(quote)
        if start < 0 or source.find(quote, start + 1) >= 0:
            raise InvalidAnalysis()
        citation.update(start=start, end=start + len(quote))
    raw = json.dumps(normalized, ensure_ascii=False)
    validate_response(raw, sources)
    return raw


def _version(value):
    if type(value) is not str or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value
    ):
        raise ProviderFailure("provider_protocol")
    return value


def _counter(value):
    if type(value) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,11}", value):
        raise ProviderFailure("provider_usage_anomaly")
    return int(value)


def checked_usage(value):
    if type(value) is not dict:
        raise ProviderFailure("provider_usage_anomaly")
    usage = {
        key: _counter(value.get(key))
        for key in ("inputTextTokens", "completionTokens", "totalTokens")
    }
    details = value.get("completionTokensDetails", {})
    if type(details) is not dict or _counter(details.get("reasoningTokens", "0")) != 0:
        raise ProviderFailure("provider_usage_anomaly")
    if usage["totalTokens"] < usage["inputTextTokens"] + usage["completionTokens"]:
        raise ProviderFailure("provider_usage_anomaly")
    return usage


def _json_reply(reply, *, limit):
    if reply.error:
        code = reply.error
        raise ProviderFailure(
            code,
            transient=code
            in {"provider_timeout", "provider_connection", "provider_transport"},
        )
    status = reply.status
    if status in (401, 403):
        raise ProviderFailure("provider_auth")
    if status == 429:
        raise ProviderFailure("provider_rate_limit", transient=True)
    if 500 <= status <= 599:
        raise ProviderFailure("provider_unavailable", transient=True)
    if status != 200:
        raise ProviderFailure("provider_rejected")
    try:
        return parse_object(reply.body, byte_limit=limit)
    except InvalidAnalysis:
        raise ProviderFailure("provider_protocol") from None


def analyze_yandex(packet, before_generate):
    payload = completion_payload(packet)
    tokens = _json_reply(request_http("tokenizeCompletion", payload), limit=4 * 1048576)
    version = _version(tokens.get("modelVersion"))
    if type(tokens.get("tokens")) is not list:
        raise ProviderFailure("provider_protocol")
    if len(tokens["tokens"]) > packet["provider_profile"]["max_input_tokens"]:
        raise ProviderFailure("input_too_large")
    # No generation can start without a committed, authorized dispatch permit.
    if before_generate is None or not before_generate():
        raise ProviderFailure("dispatch_denied")
    envelope = _json_reply(request_http("completion", payload), limit=262144)
    # The REST reference shows CompletionResponse directly; official synchronous
    # examples wrap it in "result". Accept either, never competing payloads.
    if "result" in envelope:
        if set(envelope) != {"result"} or type(envelope["result"]) is not dict:
            raise ProviderFailure("provider_protocol")
        envelope = envelope["result"]
    try:
        usage = checked_usage(envelope.get("usage"))
        returned_version = _version(envelope.get("modelVersion"))
    except ProviderFailure:
        return ProviderReply(None, None, "", "provider_usage_anomaly", True)
    if returned_version != version:
        return ProviderReply(
            None, usage, returned_version, "configuration_changed", True
        )
    alternatives = envelope.get("alternatives")
    if (
        type(alternatives) is not list
        or len(alternatives) != 1
        or type(alternatives[0]) is not dict
    ):
        return ProviderReply(None, usage, returned_version, "provider_protocol")
    alternative = alternatives[0]
    if alternative.get("status") != "ALTERNATIVE_STATUS_FINAL":
        error = (
            "provider_refusal"
            if alternative.get("status") == "ALTERNATIVE_STATUS_CONTENT_FILTER"
            else "provider_truncated"
        )
        return ProviderReply(None, usage, returned_version, error)
    message = alternative.get("message")
    if (
        type(message) is not dict
        or set(message) != {"role", "text"}
        or message.get("role") != "assistant"
    ):
        return ProviderReply(None, usage, returned_version, "provider_protocol")
    try:
        raw = normalize_response(message["text"], packet["sources"])
    except InvalidAnalysis:
        return ProviderReply(None, usage, returned_version, "invalid_response")
    return ProviderReply(raw, usage, returned_version)
