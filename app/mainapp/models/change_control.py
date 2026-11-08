"""Human decisions and revision-bound evidence, append-only in the application.

Team/actor/action/time follows the evaluated Next.js SaaS Starter activityLogs
principle (MIT, Vercel). No Drizzle source, IP tracking, or second ORM is used.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from mainapp.models.analysis import DomainRecord, AnalysisRun

CATEGORY_LABELS = {
    "included": "Входит в объём",
    "possible_change": "Возможное изменение",
    "insufficient_evidence": "Недостаточно данных",
}
STATE_LABELS = {
    "reviewed": "Решение менеджера сохранено",
    "draft": "Черновик изменения",
    "shared": "Ожидает ответа клиента",
    "accepted": "Принятие записано менеджером",
    "rejected": "Отказ записан менеджером",
    "applied": "Внесено в новую версию объёма",
}


def text_field(limit, *, blank=False):
    return models.TextField(
        max_length=limit, blank=blank, validators=[MaxLengthValidator(limit)]
    )


class ImmutableChangeRecord(DomainRecord):
    class Meta(DomainRecord.Meta):
        abstract = True

    @property
    def is_terminal(self):
        return True


class ChangeCard(ImmutableChangeRecord):
    client_request = models.OneToOneField(
        "mainapp.ClientRequest", on_delete=models.PROTECT, related_name="change_card"
    )
    created_by = models.ForeignKey("usermodel.User", on_delete=models.PROTECT)

    @property
    def current_revision(self):
        return self.revisions.order_by("-number").first()


class ChangeRevision(ImmutableChangeRecord):
    card = models.ForeignKey(
        ChangeCard, on_delete=models.PROTECT, related_name="revisions"
    )
    number = models.PositiveIntegerField()
    previous_revision = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT
    )
    analysis_run = models.ForeignKey(
        "mainapp.AnalysisRun", null=True, blank=True, on_delete=models.PROTECT
    )
    category = models.CharField(max_length=30, choices=list(CATEGORY_LABELS.items()))
    description = text_field(10000)
    rationale = text_field(2000)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    currency = models.CharField(
        max_length=3, blank=True, choices=[(x, x) for x in ("RUB", "USD", "EUR")]
    )
    deadline_effect = text_field(2000, blank=True)
    created_by = models.ForeignKey("usermodel.User", on_delete=models.PROTECT)
    submission_key = models.UUIDField()
    payload_sha256 = models.CharField(max_length=64)

    class Meta(ImmutableChangeRecord.Meta):
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["card", "number"], name="change_revision_number"
            ),
            models.UniqueConstraint(
                fields=["card", "created_by", "submission_key"],
                name="change_revision_key",
            ),
            models.CheckConstraint(
                condition=models.Q(number__gte=1), name="change_revision_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__isnull=True) | models.Q(amount__gte=0),
                name="change_amount_nonnegative",
            ),
        ]

    @property
    def category_label(self):
        return CATEGORY_LABELS.get(self.category, "")

    @property
    def last_event(self):
        return self.events.order_by("-number").first()

    @property
    def state(self):
        event = self.last_event
        return (
            event.kind
            if event
            else ("draft" if self.category == "possible_change" else "reviewed")
        )

    @property
    def state_label(self):
        return STATE_LABELS[self.state]

    @property
    def estimate_complete(self):
        return self.amount is not None and bool(
            self.currency and self.deadline_effect.strip()
        )

    @property
    def amount_label(self):
        return (
            "Не определена"
            if self.amount is None
            else f"{self.amount:.2f} {self.currency}"
        )

    def clean(self):
        if not self.description.strip() or not self.rationale.strip():
            raise ValidationError("Опишите решение и его основание.")
        if self.amount is not None and (not self.amount.is_finite() or self.amount < 0):
            raise ValidationError("Некорректная сумма.")
        if (self.amount is None) != (not self.currency):
            raise ValidationError("Укажите сумму и валюту вместе.")
        if self.category != "possible_change" and (
            self.amount is not None or self.currency or self.deadline_effect
        ):
            raise ValidationError("Оценка задаётся только для возможного изменения.")
        if self.card_id:
            previous = self.card.current_revision
            if self.number != (
                previous.number + 1 if previous else 1
            ) or self.previous_revision_id != (previous.pk if previous else None):
                raise ValidationError("Неверная последовательность редакций.")
            if ChangeEvent.objects.filter(
                card_id=self.card_id, kind="applied"
            ).exists():
                raise ValidationError("Внесённое изменение требует нового запроса.")
        if self.analysis_run_id:
            run = AnalysisRun.objects.filter(pk=self.analysis_run_id).first()
            if (
                run is None
                or run.client_request_id != self.card.client_request_id
                or run.status != "validated"
            ):
                raise ValidationError(
                    "Анализ должен быть проверен и относиться к этому запросу."
                )


class ChangeEvent(ImmutableChangeRecord):
    card = models.ForeignKey(
        ChangeCard, on_delete=models.PROTECT, related_name="events"
    )
    revision = models.ForeignKey(
        ChangeRevision, on_delete=models.PROTECT, related_name="events"
    )
    number = models.PositiveIntegerField()
    kind = models.CharField(
        max_length=12,
        choices=[
            (x, STATE_LABELS[x]) for x in ("shared", "accepted", "rejected", "applied")
        ],
    )
    created_by = models.ForeignKey("usermodel.User", on_delete=models.PROTECT)
    submission_key = models.UUIDField()
    payload_sha256 = models.CharField(max_length=64)
    sender_name = models.CharField(max_length=200, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    evidence_text = text_field(5000, blank=True)
    source_description = text_field(2000, blank=True)
    resulting_scope_version = models.OneToOneField(
        "mainapp.ScopeVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )

    class Meta(ImmutableChangeRecord.Meta):
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "number"], name="change_event_number"
            ),
            models.UniqueConstraint(
                fields=["card", "created_by", "submission_key"], name="change_event_key"
            ),
            models.UniqueConstraint(
                fields=["card"],
                condition=models.Q(kind="applied"),
                name="change_one_application",
            ),
            models.CheckConstraint(
                condition=models.Q(number__gte=1), name="change_event_positive"
            ),
        ]

    @property
    def kind_label(self):
        return STATE_LABELS[self.kind]

    def clean(self):
        if (
            self.revision.card_id != self.card_id
            or self.card.current_revision.pk != self.revision_id
        ):
            raise ValidationError(
                "Событие относится только к актуальной редакции карточки."
            )
        previous = self.revision.last_event
        if self.number != (previous.number + 1 if previous else 1):
            raise ValidationError("Неверная последовательность событий.")
        allowed = {
            None: "shared",
            "shared": ("accepted", "rejected"),
            "accepted": "applied",
        }
        prior = previous.kind if previous else None
        target = allowed.get(prior, ())
        if self.kind not in (target if isinstance(target, tuple) else (target,)):
            raise ValidationError("Недопустимый переход согласования.")
        if (
            self.revision.category != "possible_change"
            or not self.revision.estimate_complete
        ):
            raise ValidationError("Сначала полностью оцените возможное изменение.")
        response = self.kind in ("accepted", "rejected")
        if response:
            if (
                not self.sender_name.strip()
                or not self.evidence_text.strip()
                or self.occurred_at is None
                or timezone.is_naive(self.occurred_at)
                or self.occurred_at > timezone.now()
            ):
                raise ValidationError(
                    "Укажите отправителя, дату и текст основания ответа."
                )
        elif (
            self.sender_name
            or self.occurred_at
            or self.evidence_text
            or self.source_description
        ):
            raise ValidationError(
                "Основание ответа задаётся только для принятия или отказа."
            )
        if (self.kind == "applied") != bool(self.resulting_scope_version_id):
            raise ValidationError("Неверная связь новой версии.")
        if self.resulting_scope_version_id:
            source = self.card.client_request.scope_version
            result = self.resulting_scope_version
            if (
                result.project_id != source.project_id
                or result.previous_version_id != source.pk
                or result.confirmed_by_id != self.created_by_id
            ):
                raise ValidationError(
                    "Новая версия не соответствует принятому изменению."
                )
