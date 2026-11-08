"""Tenant-scoped company read models; no external services or per-row queries."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import (
    Case,
    CharField,
    Count,
    DecimalField,
    Exists,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Trim
from django.urls import reverse
from django.utils import timezone

from mainapp.models.analysis import ClientRequest
from mainapp.models.change_control import ChangeRevision, ChangeEvent
from mainapp.models.scope_control import Project, ScopeVersion
from mainapp.services.change_control import _locked
from mainapp.services.project_workflow import action_for, CHOICES
from mainapp.services.scope_control import membership_for

QUEUE_FILTERS = [
    ("open", "Требуют внимания"),
    ("decision", "Нужно решение"),
    ("draft", "Подготовить согласование"),
    ("shared", "Ждём клиента"),
    ("accepted", "Обновить объём"),
    ("stale", "Проверить версию"),
    ("all", "Все запросы"),
]


def project_query(user, team_id, *, archive="active", query=""):
    membership_for(user, team_id)
    projects = Project.objects.filter(team_id=team_id, is_demo=False)
    if archive != "all":
        projects = projects.filter(archived_at__isnull=archive != "archived")
    if query:
        matching = ClientRequest.objects.filter(
            scope_version__project_id=OuterRef("pk")
        ).filter(Q(body__icontains=query) | Q(source_description__icontains=query))
        projects = projects.annotate(request_matches=Exists(matching)).filter(
            Q(name__icontains=query) | Q(request_matches=True)
        )
    return projects.order_by("-created_at", "-pk")


def request_query(user, team_id, *, archive="active", query="", state="open"):
    projects = project_query(user, team_id, archive=archive)
    revisions = ChangeRevision.objects.filter(
        card__client_request_id=OuterRef("pk")
    ).order_by("-number")
    events = ChangeEvent.objects.filter(revision_id=OuterRef("revision_id")).order_by(
        "-number"
    )
    latest = ScopeVersion.objects.filter(
        project_id=OuterRef("scope_version__project_id")
    ).order_by("-number")
    items = (
        ClientRequest.objects.filter(scope_version__project__in=projects)
        .select_related("scope_version__project")
        .defer(
            "scope_version__body",
            "scope_version__exclusions",
            "scope_version__revision_rules",
        )
    )
    if query:
        items = items.filter(
            Q(body__icontains=query)
            | Q(source_description__icontains=query)
            | Q(scope_version__project__name__icontains=query)
        )
    items = items.annotate(
        revision_id=Subquery(revisions.values("pk")[:1]),
        card_id=Subquery(revisions.values("card_id")[:1]),
        category=Subquery(revisions.values("category")[:1]),
        amount=Subquery(revisions.values("amount")[:1]),
        currency=Subquery(revisions.values("currency")[:1]),
        deadline=Trim(Subquery(revisions.values("deadline_effect")[:1])),
        latest_scope_id=Subquery(latest.values("pk")[:1]),
    ).annotate(event_kind=Subquery(events.values("kind")[:1]))
    items = items.annotate(
        work_state=Case(
            When(event_kind="applied", then=Value("applied")),
            When(~Q(scope_version_id=F("latest_scope_id")), then=Value("stale")),
            When(revision_id__isnull=True, then=Value("decision")),
            When(category="included", then=Value("included")),
            When(category="insufficient_evidence", then=Value("unclear")),
            When(
                Q(amount__isnull=True) | Q(currency="") | Q(deadline__regex=r"^\s*$"),
                then=Value("incomplete"),
            ),
            default=Coalesce("event_kind", Value("draft")),
            output_field=CharField(),
        )
    )
    if state == "open":
        items = items.exclude(work_state__in=["applied", "included", "rejected"])
    elif state == "decision":
        items = items.filter(work_state__in=["decision", "unclear", "incomplete"])
    elif state != "all":
        items = items.filter(work_state=state)
    return items.annotate(
        priority=Case(
            When(work_state="accepted", then=Value(0)),
            When(work_state__in=["decision", "unclear", "incomplete"], then=Value(1)),
            When(work_state="draft", then=Value(2)),
            When(work_state="shared", then=Value(3)),
            When(work_state="stale", then=Value(4)),
            default=Value(5),
        )
    ).order_by("priority", "created_at", "pk")


def describe_queue(items, team_id, *, can_write):
    for item in items:
        item.state_title = CHOICES[item.work_state][1]
        item.next_action = action_for(
            item.work_state,
            team_id,
            item.scope_version.project_id,
            item.pk,
            item.card_id,
        )
        # Terminal requests are inspected, not offered a new unrelated request.
        if not can_write or item.work_state in {"applied", "included", "rejected"}:
            item.next_action = dict(
                label="Посмотреть запрос",
                url=reverse(
                    "request_detail",
                    kwargs={
                        "team_id": team_id,
                        "pk": item.scope_version.project_id,
                        "request_id": item.pk,
                    },
                ),
            )
    return items


def financial_summary(user, team_id):
    """Active real projects only. Latest revision per card, no conversion/payment."""
    items = (
        request_query(user, team_id, state="all")
        .filter(category="possible_change")
        .order_by()
    )
    complete = Q(amount__isnull=False) & ~Q(currency="") & ~Q(deadline__regex=r"^\s*$")
    agreed = Q(event_kind__in=["accepted", "applied"])
    proposed = complete & Q(work_state__in=["draft", "shared"])
    rows = (
        items.filter(proposed | agreed)
        .values("currency")
        .annotate(
            proposed=Coalesce(
                Sum("amount", filter=proposed),
                Value(Decimal("0")),
                output_field=DecimalField(),
            ),
            agreed=Coalesce(
                Sum("amount", filter=agreed),
                Value(Decimal("0")),
                output_field=DecimalField(),
            ),
            proposed_count=Count("pk", filter=proposed),
            agreed_count=Count("pk", filter=agreed),
        )
        .order_by("currency")
    )
    totals = []
    for row in rows:
        row["proposed_label"] = f'{row["proposed"]:.2f} {row["currency"]}'
        row["agreed_label"] = f'{row["agreed"]:.2f} {row["currency"]}'
        totals.append(row)
    exclusions = items.aggregate(
        incomplete=Count("pk", filter=Q(work_state="incomplete")),
        stale=Count(
            "pk",
            filter=Q(work_state="stale")
            & (
                Q(event_kind__isnull=True)
                | ~Q(event_kind__in=["accepted", "applied", "rejected"])
            ),
        ),
    )
    return dict(rows=totals, **exclusions)


def set_archived(user, team_id, project_id, *, archived, confirmed=False):
    if confirmed is not True or type(archived) is not bool:
        raise ValidationError("Подтвердите изменение архива.")
    with _locked(user, team_id, project_id) as (project, _):
        if bool(project.archived_at) != archived:
            project.archived_at = timezone.now() if archived else None
            project.save(update_fields=["archived_at", "updated_at"])
        return project
