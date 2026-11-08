from django.core.management.base import BaseCommand
from mainapp.services.analysis import reconcile_analysis


class Command(BaseCommand):
    help = "Восстановить до 100 ожидающих/прерванных тестовых анализов."

    def handle(self, *args, **options):
        counts = reconcile_analysis()
        self.stdout.write(
            "scanned={scanned} recovered={recovered} published={published}".format(
                **counts
            )
        )
