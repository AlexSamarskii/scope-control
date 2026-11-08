from django.contrib import admin
from mainapp.admin.analysis import AnalysisReadOnlyAdmin
from mainapp.models.change_control import ChangeCard, ChangeRevision, ChangeEvent


@admin.register(ChangeCard)
class ChangeCardAdmin(AnalysisReadOnlyAdmin):
    team_lookup = "client_request__scope_version__project__team_id"
    list_display = ("id", "created_at")


@admin.register(ChangeRevision)
class ChangeRevisionAdmin(AnalysisReadOnlyAdmin):
    team_lookup = "card__client_request__scope_version__project__team_id"
    list_display = ("id", "number", "category", "created_at")


@admin.register(ChangeEvent)
class ChangeEventAdmin(AnalysisReadOnlyAdmin):
    team_lookup = "card__client_request__scope_version__project__team_id"
    list_display = ("id", "kind", "created_at")
