"""Caller holds wallet→team→membership→run→attempt locks. No network here."""

from copy import deepcopy
from datetime import timezone as dt_timezone
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import BigIntegerField, Case, F, Sum, When
from django.utils import timezone

from mainapp.models.analysis import _domain_write
from mainapp.models.analysis_budget import (
    AnalysisCharge,
    BUDGET_KEY,
    GLOBAL_LIMIT,
    ProviderBudget,
)
from mainapp.services.analysis_provider import RESERVE_MICRORUB


def lock_budget():
    """Create once under a savepoint; concurrent first claims share one wallet."""
    if not ProviderBudget.objects.filter(key=BUDGET_KEY).exists():
        try:
            with transaction.atomic(), _domain_write():
                ProviderBudget.objects.create(key=BUDGET_KEY)
        except (IntegrityError, ValidationError):
            if not ProviderBudget.objects.filter(key=BUDGET_KEY).exists():
                raise
    return ProviderBudget.objects.select_for_update().get(key=BUDGET_KEY)


def used_amount(query):
    return (
        query.aggregate(
            amount=Sum(
                Case(
                    When(state="settled", then=F("charged_microrub")),
                    default=F("reserved_microrub"),
                    output_field=BigIntegerField(),
                )
            )
        )["amount"]
        or 0
    )


def can_reserve(team_id, now):
    budget = ProviderBudget.objects.get(key=BUDGET_KEY)
    if (
        budget.suspended
        or used_amount(budget.charges.all()) + RESERVE_MICRORUB > budget.limit_microrub
    ):
        return False
    midnight = now.astimezone(dt_timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    company = budget.charges.filter(
        attempt__run__client_request__scope_version__project__team_id=team_id,
        created_at__gte=midnight,
    )
    return used_amount(company) + RESERVE_MICRORUB <= GLOBAL_LIMIT


def reserve_attempt(attempt):
    with _domain_write():
        return AnalysisCharge.objects.create(
            attempt=attempt,
            budget=ProviderBudget.objects.get(key=BUDGET_KEY),
            profile=deepcopy(attempt.run.provider_profile),
            reserved_microrub=RESERVE_MICRORUB,
        )


def mark_dispatched(attempt, now):
    charge = AnalysisCharge.objects.select_for_update().get(attempt=attempt)
    if charge.state != "reserved" or charge.generation_dispatched_at is not None:
        return False
    charge.generation_dispatched_at = now
    with _domain_write():
        charge.save()
    return True


def _suspend(budget):
    if not budget.suspended:
        budget.suspended = True
        with _domain_write():
            budget.save()


def settle_receipt(attempt, reply, now):
    charge = AnalysisCharge.objects.select_for_update().get(attempt=attempt)
    budget = ProviderBudget.objects.get(pk=charge.budget_id)
    if charge.is_terminal:
        return False
    if charge.generation_dispatched_at is None:
        _suspend(budget)
        return False
    if reply.anomaly:
        _suspend(budget)
    usage = reply.usage
    if usage is None:
        finish_unknown(attempt, now)
        return False
    keys = {"inputTextTokens", "completionTokens", "totalTokens"}
    if (
        type(usage) is not dict
        or set(usage) != keys
        or any(type(v) is not int or v < 0 for v in usage.values())
    ):
        _suspend(budget)
        finish_unknown(attempt, now)
        return False
    count = max(
        usage["totalTokens"], usage["inputTextTokens"] + usage["completionTokens"]
    )
    actual = count * charge.profile["microrub_per_token"]
    if (
        actual > charge.reserved_microrub
        or usage["inputTextTokens"] > charge.profile["input_ceiling"]
        or usage["completionTokens"] > charge.profile["max_output_tokens"]
    ):
        _suspend(budget)
        finish_unknown(attempt, now)
        return False
    charge.state = "settled"
    charge.charged_microrub = actual
    charge.usage = deepcopy(usage)
    charge.model_version = reply.model_version
    charge.settled_at = now
    with _domain_write():
        charge.save()
    return True


def finish_unknown(attempt, now):
    charge = AnalysisCharge.objects.select_for_update().get(attempt=attempt)
    if charge.is_terminal:
        return
    if charge.generation_dispatched_at is None:
        # Dispatch cannot happen later: its callback requires the original live lease.
        charge.state = "settled"
        charge.charged_microrub = 0
        charge.settled_at = now
    else:
        charge.state = "uncertain"
    with _domain_write():
        charge.save()
