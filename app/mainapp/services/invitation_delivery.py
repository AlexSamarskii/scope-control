"""Durable invitation intent; SMTP ambiguity never causes automatic retransmission."""

from datetime import timedelta
from uuid import uuid4

from celery import current_app
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from mainapp.middleware_recovery import require_live
from mainapp.models import TeamInvitation


def enqueue_invitation(invitation_id):
    require_live()
    # Save before dispatch: an eager/fast worker must never be overwritten below.
    changed = TeamInvitation.objects.filter(
        pk=invitation_id, delivery_status__in=["pending", "queued"]
    ).update(delivery_status="queued", delivery_updated_at=timezone.now())
    if not changed:
        return False
    try:
        current_app.send_task(
            "send_team_invitation_email", kwargs={"invitation_id": invitation_id}
        )
    except Exception:
        # Do not log exception text: broker configuration can contain credentials.
        TeamInvitation.objects.filter(
            pk=invitation_id, delivery_status="queued"
        ).update(delivery_status="pending")
        return False
    return True


@transaction.atomic
def request_resend(invitation_id, team_id):
    require_live()
    invitation = TeamInvitation.objects.select_for_update().get(
        pk=invitation_id, team_id=team_id
    )
    if not invitation.is_valid() or not invitation.team.is_active:
        raise ValidationError("Приглашение уже недействительно.")
    # Sending has an intentionally longer uncertainty window than SMTP timeout.
    delay = 120 if invitation.delivery_status == "sending" else 60
    if invitation.delivery_updated_at > timezone.now() - timedelta(seconds=delay):
        raise ValidationError(
            "Отправка уже запрошена. Подождите две минуты перед повтором."
        )
    invitation.delivery_status = "pending"
    invitation.delivery_attempt = None
    invitation.delivery_updated_at = timezone.now()
    invitation.save(
        update_fields=["delivery_status", "delivery_attempt", "delivery_updated_at"]
    )
    transaction.on_commit(lambda: enqueue_invitation(invitation.pk))


@transaction.atomic
def claim_delivery(invitation_id):
    require_live()
    invitation = (
        TeamInvitation.objects.select_for_update().filter(pk=invitation_id).first()
    )
    if invitation is None or invitation.delivery_status not in ("pending", "queued"):
        return None
    if not invitation.is_valid() or not invitation.team.is_active:
        return None
    invitation.delivery_status = "sending"
    invitation.delivery_attempt = uuid4()
    invitation.delivery_updated_at = timezone.now()
    invitation.save(
        update_fields=["delivery_status", "delivery_attempt", "delivery_updated_at"]
    )
    return invitation


def finish_delivery(invitation, status):
    TeamInvitation.objects.filter(
        pk=invitation.pk,
        delivery_status="sending",
        delivery_attempt=invitation.delivery_attempt,
    ).update(delivery_status=status, delivery_updated_at=timezone.now())
