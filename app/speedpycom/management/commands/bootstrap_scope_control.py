from getpass import getpass

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from mainapp.models import Team, TeamMembership


class Command(BaseCommand):
    help = "Локально создать первого владельца студии. Пароль вводится интерактивно."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--team", required=True, help="Название студии")
        parser.add_argument("--slug", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        User = get_user_model()
        existing_team = Team.objects.filter(slug=options["slug"]).first()
        existing_user = User.objects.filter(email__iexact=email).first()
        if existing_team:
            if (
                existing_user
                and TeamMembership.objects.filter(
                    user=existing_user, team=existing_team, role="owner"
                ).exists()
            ):
                self.stdout.write("Владелец и студия уже настроены. Изменений нет.")
                return
            raise CommandError(
                "Студия уже существует. Автоматическая передача владения запрещена."
            )
        if existing_user:
            raise CommandError(
                "Аккаунт уже существует. Используйте проверенное приглашение."
            )
        user = User(email=email)
        user.full_clean(exclude=["password"])
        password = getpass("Пароль владельца: ")
        if password != getpass("Повторите пароль: "):
            raise CommandError("Пароли не совпадают.")
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        user.set_password(password)
        user.save()
        EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
        team = Team(name=options["team"], slug=options["slug"])
        team.full_clean()
        team.save()
        TeamMembership.objects.create(user=user, team=team, role="owner")
        self.stdout.write(
            self.style.SUCCESS(
                "Владелец и студия созданы. Пароль не записывался в вывод."
            )
        )
