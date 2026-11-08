from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field, Layout
from django import forms

from mainapp.models.analysis import AnalysisRun
from mainapp.models.change_control import CATEGORY_LABELS
from mainapp.forms.scope_control import ScopeConfirmationForm


class ChangeForm(forms.Form):
    submission_key = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    expected_event_id = forms.UUIDField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(*[Field(x) for x in self.fields])


class DecisionForm(ChangeForm):
    category = forms.ChoiceField(
        label="Моё решение",
        choices=[("", "Выберите решение"), *CATEGORY_LABELS.items()],
    )
    description = forms.CharField(
        label="Описание решения / изменения",
        max_length=10000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    rationale = forms.CharField(
        label="Моё основание решения",
        max_length=2000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    analysis_run = forms.ModelChoiceField(
        label="Проверенный анализ (необязательно)",
        queryset=AnalysisRun.objects.none(),
        required=False,
    )
    amount = forms.DecimalField(
        label="Стоимость изменения (пусто — неизвестна)",
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    currency = forms.ChoiceField(
        label="Валюта",
        choices=[("", "Не указана"), ("RUB", "RUB"), ("USD", "USD"), ("EUR", "EUR")],
        required=False,
    )
    deadline_effect = forms.CharField(
        label="Влияние на срок (введите вручную)",
        max_length=2000,
        strip=False,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    confirmed = forms.BooleanField(
        label="Решение проверено мной. Я явно сохраняю эту редакцию."
    )

    def __init__(self, *args, item, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["analysis_run"].queryset = item.analysis_runs.filter(status="validated")
        self.fields["analysis_run"].empty_label = "Без анализа"
        if not self.fields["analysis_run"].queryset.exists():
            self.fields["analysis_run"].widget = forms.HiddenInput()

    def clean(self):
        data = super().clean()
        for field in ("description", "rationale"):
            if field in data and not data[field].strip():
                self.add_error(field, "Введите текст.")
        if (data.get("amount") is None) != (not data.get("currency")):
            self.add_error("amount", "Укажите сумму и валюту вместе.")
        if data.get("category") in ("included", "insufficient_evidence") and (
            data.get("amount") is not None
            or data.get("currency")
            or data.get("deadline_effect")
        ):
            self.add_error(
                "category", "Оценка задаётся только для возможного изменения."
            )
        return data


class ChangeEventForm(ChangeForm):
    kind = forms.ChoiceField(
        label="Моё действие",
        choices=[
            ("", "Выберите действие"),
            ("shared", "Я передал условия клиенту"),
            ("accepted", "Записать принятие клиентом"),
            ("rejected", "Записать отказ клиента"),
        ],
    )
    sender_name = forms.CharField(label="Кто ответил", max_length=200, required=False)
    occurred_at = forms.DateTimeField(
        label="Когда получен ответ",
        required=False,
        help_text="Локальная дата и время с учётом часового пояса вашей компании.",
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
        ),
    )
    evidence_text = forms.CharField(
        label="Текст основания ответа",
        max_length=5000,
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    source_description = forms.CharField(
        label="Описание источника / ссылка (не загружается)",
        max_length=2000,
        required=False,
        strip=False,
    )
    confirmed = forms.BooleanField(
        label="Подтверждаю: я лично выполнил это действие и проверил основание. Это не проверка подписи или оплаты."
    )

    def clean(self):
        data = super().clean()
        if data.get("kind") in ("accepted", "rejected"):
            for field in ("sender_name", "occurred_at", "evidence_text"):
                if (
                    not data.get(field)
                    or isinstance(data[field], str)
                    and not data[field].strip()
                ):
                    self.add_error(field, "Укажите основание ответа.")
        elif data.get("kind") == "shared":
            for field in (
                "sender_name",
                "occurred_at",
                "evidence_text",
                "source_description",
            ):
                if data.get(field):
                    self.add_error(field, "Для отметки передачи оставьте поле пустым.")
        return data


class ChangeScopeForm(ScopeConfirmationForm):
    preview_token = forms.CharField(required=False, widget=forms.HiddenInput)
    submission_key = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_event_id = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["expected_version"].required = True
        self.helper.layout = Layout(*[Field(x) for x in self.fields])
