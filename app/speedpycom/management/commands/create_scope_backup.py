import json
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.core.exceptions import ValidationError
from mainapp.services.backup_storage import StorageFailure
from mainapp.services.database_backup import create_backup


class Command(BaseCommand):
    help = "Operator-only create_scope_backup; outputs metadata only."

    def handle(self, *args, **options):
        try:
            result = create_backup()
        except StorageFailure as exc:
            raise CommandError(exc.code) from None
        except (DatabaseError, ValidationError, OSError):
            raise CommandError("lifecycle_operation_failed") from None
        self.stdout.write(json.dumps(result, sort_keys=True))
