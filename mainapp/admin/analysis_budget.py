from django.contrib import admin
from mainapp.models.analysis_budget import ProviderBudget, AnalysisCharge


class OperatorFinancialAdmin(admin.ModelAdmin):
    def get_queryset(self, request):
        query = super().get_queryset(request)
        return query if request.user.is_superuser else query.none()

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProviderBudget)
class ProviderBudgetAdmin(OperatorFinancialAdmin):
    list_display = ("key", "limit_microrub", "suspended")


@admin.register(AnalysisCharge)
class AnalysisChargeAdmin(OperatorFinancialAdmin):
    list_display = (
        "id",
        "state",
        "reserved_microrub",
        "charged_microrub",
        "created_at",
    )
