"""Explicit, synthetic-only onboarding. Existing domain services, one transaction."""

from uuid import uuid4
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from mainapp.models.scope_control import Project
from mainapp.services.analysis import _lock_team, create_request
from mainapp.services.scope_control import confirm_scope
from mainapp.services.change_control import (
    record_decision,
    record_shared,
    record_client_response,
    preview_scope_change,
    apply_scope_change,
)


@transaction.atomic
def create_demo_project(user, team_id, *, confirmed=False):
    if confirmed is not True:
        raise ValidationError("Подтвердите создание учебного примера.")
    _lock_team(user, team_id)
    existing = Project.objects.filter(team_id=team_id, is_demo=True).first()
    if existing:
        return existing
    project = Project(
        team_id=team_id, name="Учебный проект · Сайт кофейни", is_demo=True
    )
    project.save()
    base = (user, team_id, project.pk)
    version = confirm_scope(
        *base,
        body="Лендинг кофейни: о нас, меню, контакты. Адаптация для телефона.",
        exclusions="Онлайн-заказ и оплата не входят.",
        revision_rules="Две итерации правок текста и дизайна.",
        confirmed=True
    )
    item = create_request(
        *base,
        version_id=version.pk,
        body="Добавьте форму бронирования столика.",
        source_description="Вымышленное письмо для учебного примера.",
        submission_key=uuid4()
    )
    revision = record_decision(
        *base,
        item.pk,
        category="possible_change",
        description="Форма бронирования столика без онлайн-оплаты.",
        rationale="В исходном объёме есть меню и контакты, но нет бронирования.",
        amount="12000.00",
        currency="RUB",
        deadline_effect="Дополнительно два рабочих дня.",
        submission_key=uuid4(),
        confirmed=True
    )
    shared = record_shared(
        *base,
        revision.card_id,
        expected_revision_id=revision.pk,
        submission_key=uuid4(),
        confirmed=True
    )
    accepted = record_client_response(
        *base,
        revision.card_id,
        expected_revision_id=revision.pk,
        expected_event_id=shared.pk,
        kind="accepted",
        sender_name="Учебный заказчик (вымышленный)",
        occurred_at=timezone.now(),
        evidence_text="Учебный ответ: согласны на форму бронирования за 12 000 ₽ и два рабочих дня. Реального согласования не было.",
        source_description="Синтетический пример, письма не отправлялись.",
        submission_key=uuid4(),
        confirmed=True
    )
    data = dict(
        expected_revision_id=revision.pk,
        expected_event_id=accepted.pk,
        expected_version=version.pk,
        body=version.body + "\nФорма бронирования столика без онлайн-оплаты.",
        exclusions=version.exclusions,
        revision_rules=version.revision_rules,
        submission_key=uuid4(),
    )
    token = preview_scope_change(*base, revision.card_id, **data)
    result = apply_scope_change(
        *base, revision.card_id, **data, preview_token=token, confirmed=True
    )
    create_request(
        *base,
        version_id=result.pk,
        body="Можно ещё добавить онлайн-оплату бронирования?",
        source_description="Учебное упражнение: сравните с исключениями, оцените допработу и пройдите согласование самостоятельно.",
        submission_key=uuid4()
    )
    return project
