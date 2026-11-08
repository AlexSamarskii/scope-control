from django.contrib import admin
from mainapp.admin.scope_control import team_ids
from mainapp.models.data_lifecycle import (
    DataLifecycleState,
    ProjectDeletion,
    DeletedAnalysisUsage,
)


class MetadataAdmin(admin.ModelAdmin):
    def get_queryset(self, request):
        query = super().get_queryset(request)
        if request.user.is_superuser:
            return query
        if self.model is not ProjectDeletion:
            return query.none()
        from mainapp.models import TeamMembership

        owner_teams = TeamMembership.objects.filter(
            user=request.user, role="owner", team_id__in=team_ids(request.user)
        ).values_list("team_id", flat=True)
        return query.filter(team_uuid__in=owner_teams)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        if self.model is not ProjectDeletion and not request.user.is_superuser:
            return False
        return obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()


admin.site.register(DataLifecycleState, MetadataAdmin)
admin.site.register(ProjectDeletion, MetadataAdmin)
admin.site.register(DeletedAnalysisUsage, MetadataAdmin)
