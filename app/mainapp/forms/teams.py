from mainapp.roles import ROLE_LABELS
from django import forms

from speedpycom.services import email_deliverability
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Div
from crispy_tailwind.layout import Submit

from mainapp.models import Team
from mainapp.timezones import (
    get_default_team_timezone,
    is_valid_timezone,
    tz_choices,
    validate_timezone,
)
from speedpycom.widgets import ImageUploadInput


class TeamCreateForm(forms.ModelForm):
    # Auto-detected from the browser (Intl API) into a hidden field; the server
    # is the authority. LENIENT: blank or invalid falls back to the default,
    # because detection is fallible and must never block team creation.
    timezone = forms.CharField(
        required=False, max_length=64, widget=forms.HiddenInput
    )

    class Meta:
        model = Team
        fields = ('name', 'slug', 'timezone')
        labels = {'name': 'Название компании', 'slug': 'Короткое имя'}
        help_texts = {'slug': 'Для адреса компании: буквы латиницей, цифры, дефис или подчёркивание. Например: moya-studiya.'}
        error_messages = {'slug': {'unique': 'Это короткое имя уже занято. Выберите другое.'}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False  # We're handling the form tag in the template
        self.helper.layout = Layout(
            Field('name', placeholder='Моя студия'),
            Field('slug', placeholder='my-team'),
            Field('timezone'),  # hidden; filled by the create page's Intl script
        )

    def clean_timezone(self):
        value = (self.cleaned_data.get('timezone') or '').strip()
        return value if is_valid_timezone(value) else get_default_team_timezone()


class InviteMemberForm(forms.Form):
    """Form for inviting a new team member"""

    email = forms.EmailField(
        label='Электронная почта',
        max_length=255,
        help_text='Адрес сотрудника, которого вы приглашаете',
        widget=forms.EmailInput(attrs={'placeholder': 'colleague@example.com'})
    )

    role = forms.ChoiceField(
        label='Роль',
        choices=[
            ('viewer', ROLE_LABELS['viewer']),
            ('member', ROLE_LABELS['member']),
            ('admin', 'Менеджер — управление компанией'),
        ],
        initial='member'
    )

    message = forms.CharField(
        label='Личное сообщение (необязательно)',
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'Добавьте пояснение для сотрудника'
        })
    )

    def __init__(self, *args, team=None, inviter_membership=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.team = team
        self.inviter_membership = inviter_membership

        # Crispy forms helper
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Div(
                Field('email', css_class='mb-4'),
                Field('role', css_class='mb-4'),
                Field('message', css_class='mb-4'),
                css_class='space-y-4'
            )
        )

        # Adjust role choices based on inviter's role
        if inviter_membership and inviter_membership.role == 'admin':
            self.fields['role'].choices = [
                ('viewer', ROLE_LABELS['viewer']),
                ('member', ROLE_LABELS['member']),
            ]

    def clean_email(self):
        """Validate email and check for existing membership"""
        from django.contrib.auth import get_user_model
        from mainapp.models import TeamInvitation, TeamMembership
        from django.utils import timezone

        email = self.cleaned_data['email'].strip().lower()

        # Deliverability BEFORE the membership lookups: an invitation is a piece
        # of mail, so an address we cannot deliver to is not an invitation, it is
        # a bounce charged against the SES account. Same validator signup uses
        # (speedpycom.services.email_deliverability), which is the whole point —
        # the check used to exist on signup alone.
        email_deliverability.validate(email)

        # Check if user already has membership
        User = get_user_model()
        user = User.objects.filter(email=email).first()
        if user:
            if TeamMembership.objects.filter(team=self.team, user=user).exists():
                raise forms.ValidationError("Этот пользователь уже состоит в компании")

        # Check for pending invitation
        if TeamInvitation.objects.filter(
            team=self.team,
            email=email,
            status='pending',
            expires_at__gt=timezone.now()
        ).exists():
            raise forms.ValidationError("Приглашение уже существует. Проверьте статус и используйте повтор отправки в списке участников.")

        return email


class UpdateMemberRoleForm(forms.Form):
    """Form for updating a team member's role"""

    role = forms.ChoiceField(
        label='Роль',
        choices=[
            ('viewer', ROLE_LABELS['viewer']),
            ('member', ROLE_LABELS['member']),
            ('admin', ROLE_LABELS['admin']),
            ('owner', ROLE_LABELS['owner']),
        ]
    )

    def __init__(self, *args, membership=None, current_user_membership=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.membership = membership
        self.current_user_membership = current_user_membership

        # Crispy forms helper
        self.helper = FormHelper()
        self.helper.form_tag = False

        if membership:
            self.fields['role'].initial = membership.role

        # Filter choices based on current user's role
        if current_user_membership:
            if current_user_membership.role == 'admin':
                # Admins can only assign member/viewer roles
                self.fields['role'].choices = [
                    ('viewer', ROLE_LABELS['viewer']),
                    ('member', ROLE_LABELS['member']),
                ]
            elif current_user_membership.role != 'owner':
                # Non-owners/admins shouldn't access this form
                self.fields['role'].choices = []


class TeamSettingsForm(forms.ModelForm):
    """Form for updating team settings (name, slug, logo, timezone)."""

    class Meta:
        model = Team
        fields = ('name', 'slug', 'logo', 'timezone')
        widgets = {
            'logo': ImageUploadInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # An explicit dropdown from the shortlist. STRICT: the value comes from
        # our own menu, so ChoiceField already rejects anything not offered; an
        # invalid value is a tampered POST. `include` keeps the team's current
        # value selectable even if it is off the shortlist.
        current = getattr(self.instance, 'timezone', '') or ''
        self.fields['timezone'] = forms.ChoiceField(
            choices=tz_choices(include=current),
            required=True,
            initial=current or None,
            help_text='Часовой пояс для дат и времени в интерфейсе.',
        )
        from django.urls import reverse
        self.fields["logo"].widget.attrs["preview_url"] = reverse("team_logo", kwargs={"team_id": self.instance.pk})
        self.fields["logo"].help_text = "До 5 МБ и 20 миллионов пикселей."
        for name, label in {"name": "Название компании", "slug": "Короткое имя", "logo": "Логотип", "timezone": "Часовой пояс"}.items():
            self.fields[name].label = label
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Div(
                Field('name', placeholder='Моя студия'),
                Field('slug', placeholder='my-team',
                      css_class='font-mono',
                      help_text='Короткое имя компании латиницей'),
                Field('logo'),
                Field('timezone'),
                css_class='space-y-4'
            )
        )

    def clean_slug(self):
        """Ensure slug is unique, excluding current instance."""
        slug = self.cleaned_data['slug']
        existing = Team.objects.filter(slug=slug).exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("Это короткое имя уже занято. Выберите другое.")
        return slug

    def clean_timezone(self):
        """Strict: reject a value that is not a resolvable IANA zone (a tampered
        POST, or a bad value the team had stored)."""
        value = self.cleaned_data['timezone']
        validate_timezone(value)
        return value

    def clean_logo(self):
        """Downscale the logo to 256px and convert it to a web-native format:
        HEIC/HEIF photos otherwise do not render in Firefox/Chrome, and a
        full-resolution upload is wasteful for something shown at a few dozen
        pixels. See speedpycom.images."""
        from mainapp.forms.profile_images import validate_display_image
        logo = validate_display_image(self.cleaned_data.get('logo'))
        if not logo or not hasattr(logo, 'read'):
            return logo  # unchanged / cleared / existing stored file
        from speedpycom.images import prepare_image

        return prepare_image(logo, 256)
