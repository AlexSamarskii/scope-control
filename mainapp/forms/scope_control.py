from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field, Layout
from django import forms


class ProjectForm(forms.Form):
    name = forms.CharField(label="Название проекта", max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(Field("name"))


class ScopeConfirmationForm(forms.Form):
    body = forms.CharField(
        label="Объём работ",
        max_length=100000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 12}),
    )
    exclusions = forms.CharField(
        label="Что не входит в объём",
        max_length=20000,
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    revision_rules = forms.CharField(
        label="Правила правок",
        max_length=20000,
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    expected_version = forms.UUIDField(required=False, widget=forms.HiddenInput)
    confirmed = forms.BooleanField(
        label="Подтверждаю: после сохранения эту версию нельзя изменить"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "expected_version",
            Field("body"),
            Field("exclusions"),
            Field("revision_rules"),
            Field("confirmed"),
        )

    def clean_body(self):
        value = self.cleaned_data["body"]
        if not value.strip():
            raise forms.ValidationError("Введите объём работ.")
        return value
