"""Read-only, request-bound navigation over the existing domain records."""

from uuid import UUID
from django.conf import settings
from django.core.exceptions import BadRequest
from django.http import Http404
from django.urls import reverse

from mainapp.models.analysis import ClientRequest
from mainapp.services.change_control import get_project
from mainapp.services.change_exports import current_cards, snapshot_state
from mainapp.services.scope_control import membership_for

LABELS = ("Объём", "Запрос", "Решение", "Согласование")
FORM_INTRO = {
    1: "Запишите работы, исключения и правила правок. Проверьте предпросмотр и отдельно подтвердите версию.",
    2: "Сохраните пожелание клиента и его исходную версию объёма. Решение можно принять вручную.",
    3: "Выберите собственное решение. Для новой работы укажите вашу оценку стоимости и срока.",
    4: "Запишите фактическую передачу или ответ клиента. Принятое изменение вносится в объём отдельным подтверждением.",
}


CHOICES = {
    "empty": (
        1,
        "Сначала подтвердите объём проекта",
        "Запишите, что вы обещали сделать: работы, исключения и правила правок. Подтверждённая версия останется в истории.",
        "Подтвердить объём",
        "scope_confirm",
    ),
    "scope": (
        2,
        "Добавьте пожелание клиента",
        "Новый запрос сохранится вместе с выбранной версией объёма. Затем вы сможете принять решение по нему.",
        "Добавить запрос клиента",
        "request_create",
    ),
    "decision": (
        3,
        "Решите, входит ли запрос в договорённости",
        "Посмотрите исходный объём и сохраните своё решение. Для новой работы укажите вашу оценку стоимости и срока.",
        "Принять решение по запросу",
        "change_create",
    ),
    "unclear": (
        3,
        "Уточните условия у клиента",
        "Недостаточно данных для уверенного решения. Уточните условия вручную и создайте новую редакцию решения; прежняя останется в истории.",
        "Уточнить решение",
        "change_revise",
    ),
    "incomplete": (
        3,
        "Дополните оценку изменения",
        "Перед согласованием укажите стоимость с валютой и влияние на срок. Пустая сумма — неизвестная оценка; 0 — ваша явная бесплатная оценка.",
        "Дополнить оценку",
        "change_revise",
    ),
    "draft": (
        4,
        "Подготовьте согласование с клиентом",
        "Проверьте черновик, скопируйте или скачайте его и передайте клиенту самостоятельно. Фактическую передачу и ответ запишите отдельно.",
        "Открыть черновик согласования",
        "change_detail",
    ),
    "shared": (
        4,
        "Ожидается ответ клиента",
        "Передача условий записана. Когда клиент ответит, сохраните, кто ответил, когда и на основании какого текста.",
        "Записать ответ клиента",
        "change_event",
    ),
    "accepted": (
        4,
        "Принятие записано. Обновите объём",
        "Принятие сохранено менеджером. Изменение ещё не входит в подтверждённый объём: подготовьте новую версию, проверьте и подтвердите её отдельно.",
        "Внести изменение в объём",
        "change_apply",
    ),
    "applied": (
        None,
        "Изменение внесено в объём",
        "Новая версия подтверждена. Исходный запрос, решение и основание принятия сохранили свою версию. Другие запросы проекта могут оставаться в работе.",
        "Добавить следующий запрос",
        "request_create",
    ),
    "included": (
        None,
        "Запрос входит в подтверждённый объём",
        "Решение менеджера сохранено. Дополнительное согласование стоимости этого запроса не требуется. Это не отметка о выполнении или оплате.",
        "Добавить другой запрос",
        "request_create",
    ),
    "rejected": (
        None,
        "Отказ клиента записан",
        "Предложенные условия не приняты, объём не менялся. Посмотрите основание отказа и при необходимости создайте новую редакцию решения.",
        "Пересмотреть решение",
        "change_revise",
    ),
    "stale": (
        3,
        "Проверьте запрос по актуальному объёму",
        "Этот запрос связан с прежней версией. Старые записи доступны, но автоматического переноса условий нет. Для продолжения создайте запрос по актуальному объёму.",
        "Создать запрос по актуальному объёму",
        "request_create",
    ),
}


def action_for(state, team_id, project_id, request_id=None, card_id=None):
    """Shared next action for both project guidance and the company queue."""
    _, _, _, label, route = CHOICES[state]
    kwargs = {"team_id": team_id, "pk": project_id}
    if route in {"change_revise", "change_detail", "change_event", "change_apply"}:
        kwargs["card_id"] = card_id
    elif route == "change_create":
        kwargs["request_id"] = request_id
    url = reverse(route, kwargs=kwargs)
    if state == "draft":
        url += "#draft"
    return dict(
        label=label, url=url, requires_write=route != "change_detail", mutates=False
    )


def _request(project, identifier, select_latest):
    query = ClientRequest.objects.filter(scope_version__project=project).select_related(
        "scope_version"
    )
    if identifier is not None:
        try:
            identifier = UUID(str(identifier))
        except (ValueError, TypeError, AttributeError):
            raise BadRequest("Некорректный идентификатор запроса.") from None
        item = query.filter(pk=identifier).first()
        if item is None:
            raise Http404("Запрос не найден.")
        return item
    return query.order_by("-created_at", "-pk").first() if select_latest else None


def _state(latest, item, snapshot):
    if latest is None:
        return "empty"
    if item is None:
        return "scope"
    if snapshot and snapshot_state(snapshot) == "applied":
        return "applied"
    if item.scope_version_id != latest.pk:
        return "stale"
    if not snapshot:
        return "decision"
    if snapshot["category"] == "included":
        return "included"
    if snapshot["category"] == "insufficient_evidence":
        return "unclear"
    # Same semantics as ChangeRevision.estimate_complete: zero is a known amount.
    if (
        snapshot["amount"] is None
        or not snapshot["currency"]
        or not snapshot["deadline_effect"].strip()
    ):
        return "incomplete"
    return snapshot_state(snapshot)


def workflow_for(
    user, team_id, project_id, request_id=None, *, select_latest=True, form_step=None
):
    project = get_project(user, team_id, project_id)
    membership = membership_for(user, team_id)
    latest = project.versions.first()
    item = _request(project, request_id, select_latest)
    card = (
        current_cards(user, team_id, project_id).filter(client_request=item).first()
        if item
        else None
    )
    snapshot = card.snapshot if card else None
    state = _state(latest, item, snapshot)
    base = {"team_id": team_id, "pk": project.pk}
    workspace = reverse("project_detail", kwargs=base)
    focused_workspace = workspace + (f"?focus={item.pk}" if item else "")
    request_url = (
        reverse("request_detail", kwargs={**base, "request_id": item.pk})
        if item
        else None
    )
    source = item.scope_version if item else latest
    source_url = (
        reverse("scope_version", kwargs={**base, "version_id": source.pk})
        if source
        else None
    )
    card_base = {**base, "card_id": card.pk} if card else None
    card_url = reverse("change_detail", kwargs=card_base) if card else None
    active, title, description, action_label, route = CHOICES[state]
    action = action_for(
        state, team_id, project.pk, item.pk if item else None, card.pk if card else None
    )
    mode = settings.SCOPE_ANALYSIS_BACKEND
    mode_label = {
        "disabled": "Ручной режим",
        "fixture": "Тестовый анализ — модель не подключена",
        "yandex": "Внешний анализ — только по вашему запуску",
    }.get(mode, "Анализ недоступен")
    step_urls = (
        source_url,
        request_url,
        card_url,
        card_url if snapshot and snapshot["category"] == "possible_change" else None,
    )
    steps = []
    for number, label in enumerate(LABELS, 1):
        current = number == (form_step or active)
        done = active is None or number < active
        status = "Текущий шаг" if current else "Сохранено" if done else "Далее"
        if state == "included" and number == 4:
            status = "Не требуется"
        if form_step:
            status = (
                "Текущий шаг"
                if current
                else "Сохранено" if step_urls[number - 1] else "Далее"
            )
        steps.append(
            dict(
                number=number,
                label=label,
                status=status,
                current=current,
                done=done and not current,
                url=step_urls[number - 1],
            )
        )
    return dict(
        project=project,
        latest_version=latest,
        request=item,
        card=card,
        snapshot=snapshot,
        state=state,
        steps=steps,
        title=title,
        description=description,
        action=action,
        can_write=membership.role in ("owner", "admin"),
        mode_label=mode_label,
        manual=mode == "disabled",
        workspace_url=focused_workspace,
        request_url=request_url,
        source_url=source_url,
        card_url=card_url,
        stale=state == "stale",
        form_step=form_step,
        form_intro=FORM_INTRO.get(form_step),
        resulting_version=(
            (snapshot.get("event") or {}).get("scope_version") if snapshot else None
        ),
    )


def describe_requests(user, team_id, project_id, items):
    """At most one annotated card query for a page, never per-row revision reads."""
    cards = {
        card.client_request_id: card
        for card in current_cards(user, team_id, project_id).filter(
            client_request_id__in=[item.pk for item in items]
        )
    }
    from mainapp.models.change_control import CATEGORY_LABELS, STATE_LABELS

    for item in items:
        card = cards.get(item.pk)
        item.workflow_label = (
            STATE_LABELS[snapshot_state(card.snapshot)]
            if card
            else "Решение ещё не принято"
        )
        if card and card.snapshot["category"] != "possible_change":
            item.workflow_label = CATEGORY_LABELS[card.snapshot["category"]]
        item.workflow_card = card
    return items
