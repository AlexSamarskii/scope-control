import json
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from mainapp.services.backup_storage import StorageFailure
from mainapp.services.data_lifecycle import purge_project


class Command(BaseCommand):
    help = "Operator-only exact receipt purge; dry-run unless --execute."

    def add_arguments(self, parser):
        parser.add_argument("receipt_id")
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--maintenance", action="store_true")
        parser.add_argument("--processes-stopped", action="store_true")
        parser.add_argument("--quiescent-since")

    def handle(self, *args, **options):
        try:
            since = (
                parse_datetime(options["quiescent_since"])
                if options["quiescent_since"]
                else None
            )
            if since is not None and timezone.is_naive(since):
                raise StorageFailure("timezone_required")
            result = purge_project(
                options["receipt_id"],
                execute=options["execute"],
                maintenance=options["maintenance"],
                processes_stopped=options["processes_stopped"],
                quiescent_since=since,
            )
        except StorageFailure as exc:
            raise CommandError(exc.code) from None
        except (DatabaseError, ValidationError, OSError, ValueError):
            raise CommandError("lifecycle_operation_failed") from None
        self.stdout.write(json.dumps(result, sort_keys=True))
