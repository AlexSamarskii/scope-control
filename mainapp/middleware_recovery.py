"""Fail-closed restored database guard; no secret/config files or caches."""

from django.core.exceptions import ValidationError
from django.db import connections, DatabaseError
from django.http import JsonResponse


def is_quarantined(alias="default"):
    if str(connections[alias].settings_dict["NAME"]).startswith("scope_restore_"):
        return True
    from mainapp.models.data_lifecycle import DataLifecycleState

    try:
        return DataLifecycleState.objects.using(alias).filter(quarantined=True).exists()
    except DatabaseError:
        return True


def require_live(alias="default"):
    if is_quarantined(alias):
        raise ValidationError("Среда восстановления изолирована. Операция запрещена.")


class RecoveryQuarantineMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if is_quarantined():
            return JsonResponse({"status": "quarantined"}, status=503)
        return self.get_response(request)
