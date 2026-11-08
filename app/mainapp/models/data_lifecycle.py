"""Minimal lifecycle metadata; never contains project names or document content."""

import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from mainapp.models.analysis import DomainRecord


class DataLifecycleState(DomainRecord):
    key = models.CharField(max_length=20, unique=True, default="installation")
    install_id = models.UUIDField(default=uuid.uuid4, editable=False)
    inventory_sequence = models.PositiveBigIntegerField(default=0)
    quarantined = models.BooleanField(default=False)
    identity_fields = ("key", "install_id", "created_at")

    class Meta(DomainRecord.Meta):
        pass

    @property
    def is_terminal(self):
        return False

    def clean(self):
        if self.key != "installation":
            raise ValidationError("Неверная установка.")

    def check_transition(self, original):
        if (
            self.inventory_sequence < original.inventory_sequence
            or original.quarantined
            and not self.quarantined
        ):
            raise ValidationError("Нельзя сбросить sequence или карантин.")


class ProjectDeletion(DomainRecord):
    project_uuid = models.UUIDField(unique=True)
    team_uuid = models.UUIDField()
    actor_uuid = models.UUIDField()
    submission_key = models.UUIDField()
    payload_sha256 = models.CharField(max_length=64)
    requested_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=12,
        choices=[("requested", "Ожидает оператора"), ("completed", "Удалён")],
        default="requested",
    )
    error_code = models.CharField(max_length=40, blank=True)
    identity_fields = (
        "project_uuid",
        "team_uuid",
        "actor_uuid",
        "submission_key",
        "payload_sha256",
        "requested_at",
        "created_at",
    )

    class Meta(DomainRecord.Meta):
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["team_uuid", "actor_uuid", "submission_key"],
                name="project_deletion_key",
            )
        ]

    @property
    def is_terminal(self):
        return self.status == "completed"

    def check_transition(self, original):
        if original.started_at and self.started_at != original.started_at:
            raise ValidationError("Нельзя изменить начало удаления.")

    def clean(self):
        if (self.status == "completed") != bool(self.completed_at):
            raise ValidationError("Неверный статус удаления.")
        if self.error_code not in ("", "operator_retry_required"):
            raise ValidationError("Неизвестный код операции.")


class DeletedAnalysisUsage(DomainRecord):
    kind = models.CharField(
        max_length=8, choices=[("run", "run"), ("attempt", "attempt")]
    )
    original_uuid = models.UUIDField()
    team_uuid = models.UUIDField()
    occurred_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    class Meta(DomainRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "original_uuid"], name="deleted_usage_identity"
            )
        ]
        indexes = [
            models.Index(
                fields=["team_uuid", "kind", "occurred_at"], name="deleted_usage_window"
            )
        ]

    @property
    def is_terminal(self):
        return True
