import json
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.core.exceptions import ValidationError
from mainapp.services.backup_storage import StorageFailure
from mainapp.services.database_backup import restore_backup


class Command(BaseCommand):
    help = "Operator-only restore_scope_backup; outputs metadata only."

    def add_arguments(self, parser):
        parser.add_argument("backup_id")
        parser.add_argument("--execute", action="store_true")

    def handle(self, *args, **options):
        try:
            result = restore_backup(options["backup_id"], execute=options["execute"])
        except StorageFailure as exc:
            raise CommandError(exc.code) from None
        except (DatabaseError, ValidationError, OSError):
            raise CommandError("lifecycle_operation_failed") from None
        self.stdout.write(json.dumps(result, sort_keys=True))
