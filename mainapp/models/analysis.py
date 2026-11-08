"""Version-bound analysis records; domain services are the only writers."""

from contextlib import contextmanager
from contextvars import ContextVar

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils import timezone

from speedpycom.models import BaseModel

_writing = ContextVar("scope_analysis_writing", default=False)


@contextmanager
def _domain_write():
    token = _writing.set(True)
    try:
        yield
    finally:
        _writing.reset(token)


class DomainQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Используйте сервис обработки запроса.")

    def bulk_update(self, *args, **kwargs):
        raise ValidationError("Пакетное изменение запрещено.")

    def bulk_create(self, *args, **kwargs):
        raise ValidationError("Используйте сервис создания запроса.")

    def delete(self):
        raise ValidationError("Удаление требует отдельного процесса.")


class DomainRecord(BaseModel):
    objects = DomainQuerySet.as_manager()
    identity_fields = ()

    class Meta:
        abstract = True
        base_manager_name = "objects"
        default_manager_name = "objects"

    def save(self, *args, **kwargs):
        if not _writing.get():
            raise ValidationError("Используйте сервис обработки запроса.")
        original = type(self).objects.filter(pk=self.pk).first()
        if original is not None:
            if isinstance(self, ClientRequest) or original.is_terminal:
                raise ValidationError("Сохранённую запись нельзя изменять.")
            if any(
                getattr(original, f) != getattr(self, f) for f in self.identity_fields
            ):
                raise ValidationError("Нельзя изменять источник и владельца записи.")
            self.check_transition(original)
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Удаление требует отдельного процесса.")


class ClientRequest(DomainRecord):
    scope_version = models.ForeignKey(
        "mainapp.ScopeVersion", on_delete=models.PROTECT, related_name="client_requests"
    )
    body = models.TextField(max_length=20000, validators=[MaxLengthValidator(20000)])
    source_description = models.TextField(
        blank=True, max_length=2000, validators=[MaxLengthValidator(2000)]
    )
    created_by = models.ForeignKey("usermodel.User", on_delete=models.PROTECT)
    submission_key = models.UUIDField()

    class Meta(DomainRecord.Meta):
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["scope_version", "created_by", "submission_key"],
                name="request_submission_unique",
            )
        ]

    def clean(self):
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValidationError({"body": "Введите текст запроса."})


class AnalysisRun(DomainRecord):
    class Status(models.TextChoices):
        QUEUED = "queued", "Ожидает обработки"
        RUNNING = "running", "Обрабатывается"
        VALIDATED = "validated", "Тестовый результат проверен"
        FAILED = "failed", "Обработка не завершена"

    client_request = models.ForeignKey(
        ClientRequest, on_delete=models.PROTECT, related_name="analysis_runs"
    )
    requested_by = models.ForeignKey("usermodel.User", on_delete=models.PROTECT)
    start_key = models.UUIDField()
    backend = models.CharField(max_length=32, default="fixture")
    schema_version = models.PositiveIntegerField(default=1)
    fixture_revision = models.CharField(max_length=64, default="scope-fixtures-v1")
    provider_profile = models.JSONField(default=dict, blank=True)
    external_processing_confirmed_at = models.DateTimeField(null=True, blank=True)
    input_sha256 = models.CharField(max_length=64)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    validated_result = models.JSONField(null=True, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    identity_fields = (
        "client_request_id",
        "requested_by_id",
        "start_key",
        "backend",
        "schema_version",
        "fixture_revision",
        "input_sha256",
        "provider_profile",
        "external_processing_confirmed_at",
        "created_at",
    )

    class Meta(DomainRecord.Meta):
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["client_request", "requested_by", "start_key"],
                name="analysis_start_unique",
            ),
            models.UniqueConstraint(
                fields=["client_request"],
                condition=models.Q(status__in=["queued", "running"]),
                name="analysis_one_active_request",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_count__lte=3), name="analysis_attempt_limit"
            ),
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="analysis_due_idx")
        ]

    @property
    def is_terminal(self):
        return self.status in (self.Status.VALIDATED, self.Status.FAILED)

    @property
    def category_label(self):
        return {
            "included": "Входит в объём",
            "possible_change": "Возможное изменение",
            "insufficient_evidence": "Недостаточно данных",
        }.get((self.validated_result or {}).get("category"), "")

    @property
    def result_status_label(self):
        if self.backend == "yandex" and self.status == "validated":
            return "Ответ модели прошёл проверку источников"
        return self.get_status_display()

    @property
    def error_label(self):
        return {
            "access_revoked": "Доступ автора запуска прекращён. Результат не опубликован.",
            "quota_exceeded": "Достигнут дневной лимит попыток компании.",
            "attempts_exhausted": "Исчерпаны три попытки обработки.",
            "transient_error": "Временный сбой обработки.",
            "invalid_response": "Ответ не прошёл проверку. Результат не опубликован.",
            "worker_lost": "Обработка прервалась. Запрос сохранён и ожидает повторной попытки.",
            "backend_disabled": "Анализ отключён.",
            "configuration_changed": "Конфигурация модели или тариф изменились. Нужна проверка оператора.",
            "configuration_missing": "Worker не настроен для внешней модели.",
            "budget_exceeded": "Недостаточно денежного лимита на следующую попытку.",
            "provider_usage_anomaly": "Неожиданные данные расхода. Дальнейшие вызовы приостановлены.",
            "provider_auth": "Проверьте доступ сервисного аккаунта к модели.",
            "provider_rate_limit": "Провайдер ограничил частоту запросов.",
            "provider_unavailable": "Провайдер временно недоступен.",
            "provider_timeout": "Время ожидания модели истекло. Резерв расхода может сохраняться.",
            "provider_refusal": "Модель отказалась обрабатывать запрос.",
            "provider_truncated": "Ответ модели неполный и не опубликован.",
            "input_too_large": "Объём превышает предел модели. Тексты сохранены без усечения.",
            "provider_protocol": "Ответ провайдера не прошёл проверку протокола.",
        }.get(self.error_code, "Обработка не завершена. Исходный запрос сохранён.")

    def check_transition(self, original):
        allowed = {
            "queued": {"running", "failed"},
            "running": {"queued", "validated", "failed"},
        }
        if self.status not in allowed.get(original.status, set()):
            raise ValidationError("Недопустимый переход обработки.")
        expected = original.attempt_count + (self.status == "running")
        if self.attempt_count != expected:
            raise ValidationError("Нельзя сбрасывать счётчик попыток.")

    def clean(self):
        if self.backend not in {"fixture", "yandex"} or self.schema_version != 1:
            raise ValidationError("Неизвестный адаптер или версия схемы.")
        if self.backend == "fixture" and (
            self.fixture_revision != "scope-fixtures-v1"
            or self.provider_profile
            or self.external_processing_confirmed_at
        ):
            raise ValidationError("Неверный профиль тестового адаптера.")
        if self.backend == "yandex" and (
            type(self.provider_profile) is not dict
            or not self.provider_profile
            or self.external_processing_confirmed_at is None
        ):
            raise ValidationError("Внешнему анализу нужны профиль и явное согласие.")
        if self.status == self.Status.RUNNING and not (
            self.lease_token and self.lease_expires_at
        ):
            raise ValidationError("Обработке нужна действующая аренда.")
        if self.status != self.Status.VALIDATED and self.validated_result is not None:
            raise ValidationError("Непроверенный результат сохранять нельзя.")
        if self.is_terminal != (self.finished_at is not None):
            raise ValidationError("Неверное время завершения.")


class AnalysisAttempt(DomainRecord):
    run = models.ForeignKey(
        AnalysisRun, on_delete=models.PROTECT, related_name="attempts"
    )
    number = models.PositiveIntegerField()
    lease_token = models.UUIDField(unique=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(
        max_length=24,
        choices=[
            (v, v)
            for v in (
                "running",
                "validated",
                "transient_error",
                "invalid_response",
                "worker_lost",
                "access_revoked",
                "internal_error",
            )
        ],
        default="running",
    )
    error_code = models.CharField(max_length=40, blank=True)
    identity_fields = ("run_id", "number", "lease_token", "started_at", "created_at")

    class Meta(DomainRecord.Meta):
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "number"], name="analysis_attempt_unique"
            )
        ]

    @property
    def is_terminal(self):
        return self.outcome != "running"

    @property
    def outcome_label(self):
        if self.outcome == "validated" and self.run.backend == "yandex":
            return "Ответ прошёл проверку"
        return {
            "running": "Обрабатывается",
            "validated": "Тестовый ответ проверен",
            "transient_error": "Временный сбой",
            "invalid_response": "Ответ отклонён",
            "worker_lost": "Процесс прерван",
            "access_revoked": "Доступ прекращён",
            "internal_error": "Ошибка обработки",
        }.get(self.outcome, "Ошибка обработки")

    def check_transition(self, original):
        if not self.is_terminal or self.finished_at is None:
            raise ValidationError("Попытку можно только завершить.")

    def clean(self):
        if self.is_terminal != (self.finished_at is not None):
            raise ValidationError("Неверное завершение попытки.")
