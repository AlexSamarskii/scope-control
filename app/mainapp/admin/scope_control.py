from django.contrib import admin
from django.db.models import Q
from django.utils import timezone

from mainapp.models import TeamMembership
from mainapp.models.scope_control import Project, ScopeVersion


def team_ids(user):
    return (
        TeamMembership.objects.filter(user=user, team__is_active=True)
        .filter(
            Q(access_expires_at__isnull=True) | Q(access_expires_at__gt=timezone.now())
        )
        .values_list("team_id", flat=True)
    )


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "team", "created_at")

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return (
            queryset
            if request.user.is_superuser
            else queryset.filter(team_id__in=team_ids(request.user))
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()


@admin.register(ScopeVersion)
class ScopeVersionAdmin(admin.ModelAdmin):
    list_display = ("project", "number", "confirmed_by", "confirmed_at")

    def get_queryset(self, request):
        queryset = (
            super()
            .get_queryset(request)
            .filter(project__deletion_requested_at__isnull=True)
        )
        return (
            queryset
            if request.user.is_superuser
            else queryset.filter(project__team_id__in=team_ids(request.user))
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()
