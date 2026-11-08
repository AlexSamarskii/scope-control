"""Authorization and state transitions; input identifiers never grant access.

Tenant-scoped lookup follows the principle evaluated in Apptension's
UpdateTenantDependentModelMutation (MIT). No Graphene dependency is imported.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from mainapp.models import TeamMembership
from mainapp.models.scope_control import Project, ScopeVersion


class StaleScopeVersion(ValidationError):
    pass


def membership_for(user, team_id, *, write=False, lock=False):
    from mainapp.middleware_recovery import require_live

    require_live()
    if not user.is_authenticated or not user.is_active:
        raise Http404("Компания не найдена.")
    queryset = TeamMembership.objects.filter(
        user=user, team_id=team_id, team__is_active=True
    ).filter(
        Q(access_expires_at__isnull=True) | Q(access_expires_at__gt=timezone.now())
    )
    if lock:
        queryset = queryset.select_for_update()
    membership = queryset.first()
    if membership is None:
        raise Http404("Компания не найдена.")
    if write and membership.role not in ("owner", "admin"):
        raise PermissionDenied("Подтверждать объём может владелец или менеджер.")
    return membership


@transaction.atomic
def create_project(user, team_id, *, name):
    membership_for(user, team_id, write=True, lock=True)
    project = Project(team_id=team_id, name=name)
    project.save()
    return project


@transaction.atomic
def rename_project(user, team_id, project_id, *, name):
    membership_for(user, team_id, write=True, lock=True)
    project = (
        Project.objects.select_for_update()
        .filter(pk=project_id, team_id=team_id)
        .first()
    )
    if project is None:
        raise Http404("Проект не найден.")
    project.name = name
    project.save(update_fields=["name", "updated_at"])
    return project


@transaction.atomic
def confirm_scope(
    user,
    team_id,
    project_id,
    *,
    expected_version=None,
    body,
    exclusions="",
    revision_rules="",
    confirmed=False
):
    membership_for(user, team_id, write=True, lock=True)
    project = (
        Project.objects.select_for_update()
        .filter(pk=project_id, team_id=team_id)
        .first()
    )
    if project is None:
        raise Http404("Проект не найден.")
    if confirmed is not True:
        raise ValidationError("Подтвердите сохранение неизменяемой версии.")
    previous = project.versions.first()
    current_id = str(previous.pk) if previous else ""
    if str(expected_version or "") != current_id:
        raise StaleScopeVersion(
            "Объём уже изменился. Откройте актуальную версию и проверьте ввод."
        )
    snapshot = ScopeVersion(
        project=project,
        number=previous.number + 1 if previous else 1,
        previous_version=previous,
        body=body,
        exclusions=exclusions,
        revision_rules=revision_rules,
        confirmed_by=user,
    )
    snapshot._human_confirmation = True
    snapshot.save()
    return snapshot
