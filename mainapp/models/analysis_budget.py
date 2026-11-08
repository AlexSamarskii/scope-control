"""Financial bookkeeping is separate from immutable closed domain attempts."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from mainapp.models.analysis import DomainRecord

BUDGET_KEY = "primary-ai-account"
GLOBAL_LIMIT = 100_000_000  # microrub, cumulative, not a daily credit.


class ProviderBudget(DomainRecord):
    key = models.CharField(max_length=50, unique=True, default=BUDGET_KEY)
    limit_microrub = models.PositiveBigIntegerField(default=GLOBAL_LIMIT)
    suspended = models.BooleanField(default=False)
    identity_fields = ("key", "limit_microrub", "created_at")

    class Meta(DomainRecord.Meta):
        pass

    @property
    def is_terminal(self):
        return False

    def check_transition(self, original):
        if original.suspended and not self.suspended:
            raise ValidationError("Возобновление бюджета требует отдельного решения.")

    def clean(self):
        if self.key != BUDGET_KEY or self.limit_microrub != GLOBAL_LIMIT:
            raise ValidationError("Начальный общий бюджет ограничен 100 ₽.")


class AnalysisCharge(DomainRecord):
    attempt = models.OneToOneField(
        "mainapp.AnalysisAttempt",
        on_delete=models.SET_NULL,
        related_name="charge",
        null=True,
        blank=True,
    )
    original_attempt_uuid = models.UUIDField(default=uuid.uuid4, editable=False)
    budget = models.ForeignKey(
        ProviderBudget, on_delete=models.PROTECT, related_name="charges"
    )
    profile = models.JSONField()
    reserved_microrub = models.PositiveBigIntegerField()
    charged_microrub = models.PositiveBigIntegerField(null=True, blank=True)
    state = models.CharField(
        max_length=12,
        choices=[(x, x) for x in ("reserved", "uncertain", "settled")],
        default="reserved",
    )
    generation_dispatched_at = models.DateTimeField(null=True, blank=True)
    usage = models.JSONField(null=True, blank=True)
    model_version = models.CharField(max_length=128, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    identity_fields = (
        "attempt_id",
        "original_attempt_uuid",
        "budget_id",
        "profile",
        "reserved_microrub",
        "created_at",
    )

    class Meta(DomainRecord.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(charged_microrub__isnull=True)
                | models.Q(charged_microrub__lte=models.F("reserved_microrub")),
                name="analysis_charge_not_above_reserve",
            )
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and self.attempt_id:
            self.original_attempt_uuid = self.attempt_id
        return super().save(*args, **kwargs)

    @property
    def is_terminal(self):
        return self.state == "settled"

    @property
    def amount_rub(self):
        from decimal import Decimal

        amount = self.charged_microrub if self.is_terminal else self.reserved_microrub
        return Decimal(amount) / 1_000_000

    @property
    def state_label(self):
        return {
            "reserved": "Резерв лимита",
            "uncertain": "Расход неизвестен — резерв сохранён",
            "settled": "Учтённый расход",
        }.get(self.state, "Резерв лимита")

    def check_transition(self, original):
        if (
            original.generation_dispatched_at is not None
            and self.generation_dispatched_at != original.generation_dispatched_at
        ):
            raise ValidationError("Нельзя изменить факт отправки.")
        if original.state == "uncertain" and self.state not in ("uncertain", "settled"):
            raise ValidationError("Нельзя сбросить неопределённый расход.")

    def clean(self):
        if self.state == "settled":
            if (
                self.charged_microrub is None
                or self.settled_at is None
                or self.charged_microrub > self.reserved_microrub
            ):
                raise ValidationError("Неверное завершение учёта расходов.")
        elif self.charged_microrub is not None or self.settled_at is not None:
            raise ValidationError("Неизвестный расход не является списанием.")
