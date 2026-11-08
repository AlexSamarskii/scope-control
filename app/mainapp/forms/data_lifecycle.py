from django import forms


class ProjectDeletionForm(forms.Form):
    name = forms.CharField(label="Точное название проекта", max_length=200, strip=False)
    submission_key = forms.UUIDField(widget=forms.HiddenInput)
    preview_token = forms.CharField(
        widget=forms.HiddenInput, required=False, max_length=4096
    )
    confirmed = forms.BooleanField(
        label="Подтверждаю необратимый запрос удаления", required=False, initial=False
    )
