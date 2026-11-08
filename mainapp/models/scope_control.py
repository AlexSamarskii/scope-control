"""Scope Control records. Confirmation snapshots are append-only in the app."""

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils import timezone

from mainapp.models.teams import TeamModel
from speedpycom.models import BaseModel


class ProjectQuerySet(models.QuerySet):
    def bulk_create(self, *args, **kwargs):
        raise ValidationError("Используйте создание проекта с проверкой владельца.")

    def bulk_update(self, objs, fields, **kwargs):
        if {"team", "team_id", "deletion_requested_at", "is_demo"}.intersection(fields):
            raise ValidationError("Нельзя менять идентичность проекта или marker.")
        return super().bulk_update(objs, fields, **kwargs)

    def update(self, **kwargs):
        if "is_demo" in kwargs:
            raise ValidationError("Учебный проект нельзя превратить в рабочий.")
        if "deletion_requested_at" in kwargs:
            raise ValidationError("Используйте запрос владельца на удаление.")
        if {"team", "team_id"}.intersection(kwargs):
            raise ValidationError("Нельзя переносить проект в другую компанию.")
        return super().update(**kwargs)


class ActiveProjectManager(models.Manager.from_queryset(ProjectQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(deletion_requested_at__isnull=True)


class Project(TeamModel):
    name = models.CharField("Название проекта", max_length=200)
    deletion_requested_at = models.DateTimeField(null=True, blank=True, editable=False)
    archived_at = models.DateTimeField(null=True, blank=True, editable=False)
    is_demo = models.BooleanField(default=False, editable=False)
    objects = ActiveProjectManager()
    all_objects = ProjectQuerySet.as_manager()

    class Meta:
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["team"],
                condition=models.Q(is_demo=True, deletion_requested_at__isnull=True),
                name="one_live_demo_per_team",
            ),
        ]
        verbose_name = "Проект"
        verbose_name_plural = "Проекты"

    def save(self, *args, **kwargs):
        original = (
            type(self)
            .all_objects.filter(pk=self.pk)
            .values("team_id", "deletion_requested_at", "is_demo")
            .first()
        )
        if self.deletion_requested_at != (
            original["deletion_requested_at"] if original else None
        ):
            raise ValidationError("Marker удаления меняет только lifecycle service.")
        if original is not None and original["team_id"] != self.team_id:
            raise ValidationError("Нельзя переносить проект в другую компанию.")
        if original and original["is_demo"] != self.is_demo:
            raise ValidationError("Учебный проект нельзя превратить в рабочий.")
        if not self.name.strip():
            raise ValidationError({"name": "Введите название проекта."})
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class SnapshotQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Подтверждённую версию нельзя изменять.")

    def bulk_update(self, *args, **kwargs):
        raise ValidationError("Подтверждённую версию нельзя изменять.")

    def bulk_create(self, *args, **kwargs):
        raise ValidationError("Используйте явное подтверждение версии.")

    def delete(self):
        raise ValidationError("Подтверждённую версию нельзя удалять.")


class ScopeVersion(BaseModel):
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="versions"
    )
    number = models.PositiveIntegerField()
    body = models.TextField(
        "Объём работ", max_length=100000, validators=[MaxLengthValidator(100000)]
    )
    exclusions = models.TextField(
        "Исключения",
        max_length=20000,
        blank=True,
        validators=[MaxLengthValidator(20000)],
    )
    revision_rules = models.TextField(
        "Правила правок",
        max_length=20000,
        blank=True,
        validators=[MaxLengthValidator(20000)],
    )
    confirmed_by = models.ForeignKey("usermodel.User", on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(default=timezone.now, editable=False)
    previous_version = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True
    )
    objects = SnapshotQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "number"], name="scope_project_version_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(number__gte=1), name="scope_version_positive"
            ),
        ]
        verbose_name = "Подтверждённая версия"
        verbose_name_plural = "Подтверждённые версии"

    def clean(self):
        if not self.body.strip():
            raise ValidationError({"body": "Введите объём работ."})
        if (
            self.previous_version_id
            and self.previous_version.project_id != self.project_id
        ):
            raise ValidationError(
                "Предыдущая версия должна принадлежать тому же проекту."
            )

    def save(self, *args, **kwargs):
        if not self._state.adding or type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Подтверждённую версию нельзя изменять.")
        if not getattr(self, "_human_confirmation", False):
            raise ValidationError("Используйте явное подтверждение версии.")
        self.full_clean()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Подтверждённую версию нельзя удалять.")

    def __str__(self):
        return f"Версия {self.number}"
