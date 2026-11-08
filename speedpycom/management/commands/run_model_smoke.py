"""Explicit synthetic commissioning, never a silent fallback or a key printer."""

import json
import time
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from mainapp.models.analysis import AnalysisRun
from mainapp.models.scope_control import Project
from mainapp.services.analysis import create_request, start_analysis
from mainapp.services.analysis_provider import public_profile
from mainapp.services.scope_control import confirm_scope, membership_for
from usermodel.models import User

SYNTHETIC_SCOPE = "Синтетический проект проверки модели: разработка одной главной страницы и формы обратной связи."
SYNTHETIC_EXCLUSIONS = "Корзина, каталог товаров и оплата не входят."


class Command(BaseCommand):
    help = "Синтетический запрос в Yandex через настоящий worker. Только отдельный пустой/синтетический проект. Общий лимит 100 ₽."

    def add_arguments(self, parser):
        parser.add_argument("--team-id", required=True)
        parser.add_argument("--project-id", required=True)
        parser.add_argument("--user-id", required=True)

    def handle(self, *args, **options):
        if settings.SCOPE_ANALYSIS_BACKEND != "yandex":
            raise CommandError(
                "blocked: включите yandex backend; fixture не проверяет подключение"
            )
        try:
            public_profile()
            user = User.objects.get(pk=options["user_id"])
            membership_for(user, options["team_id"], write=True)
            project = Project.objects.get(
                pk=options["project_id"], team_id=options["team_id"]
            )
            version = project.versions.first()
            if version is None:
                version = confirm_scope(
                    user,
                    options["team_id"],
                    project.pk,
                    body=SYNTHETIC_SCOPE,
                    exclusions=SYNTHETIC_EXCLUSIONS,
                    confirmed=True,
                )
            if (
                version.body != SYNTHETIC_SCOPE
                or version.exclusions != SYNTHETIC_EXCLUSIONS
                or version.revision_rules
            ):
                raise CommandError(
                    "blocked: используйте отдельный пустой проект; существующие материалы не отправлены"
                )
            item = create_request(
                user,
                options["team_id"],
                project.pk,
                version_id=version.pk,
                body="Синтетический запрос: добавьте предусмотренную форму обратной связи.",
                source_description="Синтетическая проверка подключения, не материалы клиента.",
                submission_key=uuid4(),
            )
            run = start_analysis(
                user,
                options["team_id"],
                project.pk,
                item.pk,
                start_key=uuid4(),
                external_processing_confirmed=True,
            )
        except CommandError:
            raise
        except Exception:
            raise CommandError(
                "blocked: проверьте конфигурацию, UUID проекта и действующие права пользователя"
            ) from None
        self.stdout.write(
            json.dumps(
                {"status": "queued", "run_id": str(run.pk), "request_id": str(item.pk)},
                ensure_ascii=False,
            )
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            run = AnalysisRun.objects.get(pk=run.pk)
            if run.is_terminal:
                charges = []
                for attempt in run.attempts.select_related("charge"):
                    charge = getattr(attempt, "charge", None)
                    if charge:
                        charges.append(
                            {
                                "state": charge.state,
                                "reserved_microrub": charge.reserved_microrub,
                                "charged_microrub": charge.charged_microrub,
                                "usage": charge.usage,
                                "model_version": charge.model_version,
                            }
                        )
                success = (
                    run.status == "validated"
                    and bool(charges)
                    and all(c["state"] == "settled" for c in charges)
                )
                self.stdout.write(
                    json.dumps(
                        {
                            "status": "pass" if success else "blocked",
                            "run_id": str(run.pk),
                            "category": (run.validated_result or {}).get("category"),
                            "error_code": run.error_code,
                            "charges": charges,
                        },
                        ensure_ascii=False,
                    )
                )
                if not success:
                    raise CommandError(
                        "blocked: реальный успешно проверенный ответ не получен; смотрите безопасный код состояния"
                    )
                return
            time.sleep(1)
        raise CommandError(
            "blocked: worker не завершил запуск за 600 s; исходный запрос и состояние сохранены"
        )
