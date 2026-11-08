import json
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.core.exceptions import ValidationError
from mainapp.services.backup_storage import StorageFailure
from mainapp.services.database_backup import verify_recovery


class Command(BaseCommand):
    help = "Operator-only verify_scope_recovery; outputs metadata only."

    def handle(self, *args, **options):
        try:
            result = verify_recovery()
        except StorageFailure as exc:
            raise CommandError(exc.code) from None
        except (DatabaseError, ValidationError, OSError):
            raise CommandError("lifecycle_operation_failed") from None
        self.stdout.write(json.dumps(result, sort_keys=True))
