from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field, Layout
from django import forms
from mainapp.models.scope_control import ScopeVersion


class ClientRequestForm(forms.Form):
    version = forms.ModelChoiceField(
        label="Подтверждённая версия объёма", queryset=ScopeVersion.objects.none()
    )
    body = forms.CharField(
        label="Текст запроса клиента",
        max_length=20000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 8}),
    )
    source_description = forms.CharField(
        label="Описание источника или ссылка (не загружается)",
        max_length=2000,
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    submission_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["version"].queryset = project.versions.all()
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "submission_key",
            Field("version"),
            Field("body"),
            Field("source_description"),
        )

    def clean_body(self):
        value = self.cleaned_data["body"]
        if not value.strip():
            raise forms.ValidationError("Введите текст запроса.")
        return value


class StartAnalysisForm(forms.Form):
    start_key = forms.UUIDField(widget=forms.HiddenInput)
    external_processing_confirmed = forms.BooleanField(
        required=False,
        label="Подтверждаю: данные синтетические. Разрешаю отправить текст запроса и выбранную версию объёма в Yandex AI Studio для анализа.",
    )

    def __init__(self, *args, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        if external:
            self.fields["external_processing_confirmed"].required = True
        else:
            del self.fields["external_processing_confirmed"]
