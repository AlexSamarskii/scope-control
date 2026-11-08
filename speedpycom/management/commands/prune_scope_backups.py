import json
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.core.exceptions import ValidationError
from mainapp.services.backup_storage import StorageFailure
from mainapp.services.database_backup import prune_backups


class Command(BaseCommand):
    help = "Operator-only prune_scope_backups; outputs metadata only."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--check-age", action="store_true")

    def handle(self, *args, **options):
        try:
            result = prune_backups(
                execute=options["execute"], check_age=options["check_age"]
            )
        except StorageFailure as exc:
            raise CommandError(exc.code) from None
        except (DatabaseError, ValidationError, OSError):
            raise CommandError("lifecycle_operation_failed") from None
        self.stdout.write(json.dumps(result, sort_keys=True))
