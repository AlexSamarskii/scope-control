from django.contrib import admin
from mainapp.admin.scope_control import team_ids
from mainapp.models.analysis import ClientRequest, AnalysisRun, AnalysisAttempt


class AnalysisReadOnlyAdmin(admin.ModelAdmin):
    team_lookup = "scope_version__project__team_id"

    def get_queryset(self, request):
        query = (
            super()
            .get_queryset(request)
            .filter(
                **{
                    self.team_lookup.rsplit("__team_id", 1)[0]
                    + "__deletion_requested_at__isnull": True
                }
            )
        )
        if request.user.is_superuser:
            return query
        return query.filter(**{self.team_lookup + "__in": team_ids(request.user)})

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()


@admin.register(ClientRequest)
class ClientRequestAdmin(AnalysisReadOnlyAdmin):
    list_display = ("id", "scope_version", "created_at")


@admin.register(AnalysisRun)
class AnalysisRunAdmin(AnalysisReadOnlyAdmin):
    team_lookup = "client_request__scope_version__project__team_id"
    list_display = ("id", "status", "attempt_count", "created_at")


@admin.register(AnalysisAttempt)
class AnalysisAttemptAdmin(AnalysisReadOnlyAdmin):
    team_lookup = "run__client_request__scope_version__project__team_id"
    list_display = ("id", "number", "outcome", "started_at")
