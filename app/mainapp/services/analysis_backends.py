"""Closed backend registry; no automatic provider fallback."""

import json
from django.conf import settings
from django.core.exceptions import ValidationError


class TransientAnalysisError(Exception):
    """Explicit retryable failure; its message is never logged or persisted."""


def fixture_response(packet):
    return json.dumps(
        {
            "schema_version": 1,
            "category": "insufficient_evidence",
            "rationale": "Тестовый анализ: модель не подключена. Классификация запроса не выполнялась.",
            "citations": [],
            "questions": [
                "Какое основание в подтверждённом объёме относится к этому запросу?"
            ],
        },
        ensure_ascii=False,
    )


def get_backend(run=None, *, before_generate=None):
    from mainapp.middleware_recovery import require_live

    require_live()
    selected = settings.SCOPE_ANALYSIS_BACKEND
    if run is not None and selected != run.backend:
        raise ValidationError("Конфигурация адаптера изменилась или отключена.")
    if selected == "fixture":
        return fixture_response
    if selected == "yandex":
        from functools import partial
        from mainapp.services.analysis_provider import (
            analyze_yandex,
            public_profile,
            check_profile,
        )

        profile = public_profile()
        if run is not None:
            check_profile(run.provider_profile)
        return partial(analyze_yandex, before_generate=before_generate)
    raise ValidationError("Тестовый анализ отключён; реальная модель не подключена.")
