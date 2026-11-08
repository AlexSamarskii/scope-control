from django.core.management.base import BaseCommand
from django.utils import timezone
from mainapp.models import TeamInvitation
from mainapp.services.invitation_delivery import enqueue_invitation


class Command(BaseCommand):
    help = "Retry up to 100 pending invitation queue intents; never retry uncertain SMTP automatically."

    def handle(self, *args, **options):
        ids = list(
            TeamInvitation.objects.filter(
                status="pending",
                team__is_active=True,
                expires_at__gt=timezone.now(),
                delivery_status__in=["pending", "queued"],
            )
            .order_by("delivery_updated_at")
            .values_list("pk", flat=True)[:100]
        )
        queued = sum(enqueue_invitation(pk) for pk in ids)
        self.stdout.write(f"Queued: {queued}; inspected: {len(ids)}")
