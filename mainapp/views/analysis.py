from uuid import uuid4
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView, FormView
from django.http import HttpResponse

from mainapp.forms.analysis import ClientRequestForm, StartAnalysisForm
from mainapp.models.analysis import ClientRequest
from mainapp.models.scope_control import Project
from mainapp.services.analysis import (
    AnalysisConflict,
    AnalysisQuota,
    create_request,
    start_analysis,
)
from mainapp.views.teams import TeamAdminRequiredMixin, TeamViewMixin
from mainapp.services.project_workflow import workflow_for


class RequestCreateView(TeamAdminRequiredMixin, FormView):
    template_name = "mainapp/scope_control/request_form.html"
    form_class = ClientRequestForm

    def get_project(self):
        return get_object_or_404(Project, pk=self.kwargs["pk"], team=self.team)

    def get_initial(self):
        return {
            "submission_key": uuid4(),
            "version": self.get_project().versions.first(),
        }

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "project": self.get_project()}

    def get_context_data(self, **kwargs):
        return {
            **super().get_context_data(**kwargs),
            "project": self.get_project(),
            "external_mode": settings.SCOPE_ANALYSIS_BACKEND == "yandex",
            "workflow": workflow_for(
                self.request.user,
                self.team.pk,
                self.kwargs["pk"],
                select_latest=False,
                form_step=2,
            ),
        }

    def form_valid(self, form):
        data = form.cleaned_data.copy()
        data["version_id"] = data.pop("version").pk
        try:
            item = create_request(
                self.request.user, self.team.pk, self.kwargs["pk"], **data
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self.render_to_response(
                self.get_context_data(form=form),
                status=409 if isinstance(exc, AnalysisConflict) else 400,
            )
        return redirect(
            "request_detail",
            team_id=self.team.pk,
            pk=self.kwargs["pk"],
            request_id=item.pk,
        )


class RequestDetailView(TeamViewMixin, DetailView):
    template_name = "mainapp/scope_control/request_detail.html"
    context_object_name = "client_request"
    pk_url_kwarg = "request_id"

    def get_queryset(self):
        return ClientRequest.objects.select_related(
            "scope_version__project", "created_by"
        ).filter(
            scope_version__project_id=self.kwargs["pk"],
            scope_version__project__team=self.team,
            scope_version__project__deletion_requested_at__isnull=True,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["workflow"] = workflow_for(
            self.request.user, self.team.pk, self.kwargs["pk"], self.object.pk
        )
        context["project"] = self.object.scope_version.project
        context["runs"] = Paginator(
            self.object.analysis_runs.prefetch_related("attempts__charge"), 25
        ).get_page(self.request.GET.get("page"))
        for run in context["runs"]:
            run.presented_citations = [
                {
                    "quote": c["quote"],
                    "label": (
                        "Запрос клиента"
                        if c["source_id"].startswith("request:")
                        else {
                            "body": "Объём работ",
                            "exclusions": "Исключения",
                            "revision_rules": "Правила правок",
                        }[c["source_id"].rsplit(":", 1)[-1]]
                    ),
                }
                for c in (run.validated_result or {}).get("citations", [])
            ]
        context["analysis_enabled"] = settings.SCOPE_ANALYSIS_BACKEND in {
            "fixture",
            "yandex",
        }
        context["external_mode"] = settings.SCOPE_ANALYSIS_BACKEND == "yandex"
        context["active_run"] = self.object.analysis_runs.filter(
            status__in=["queued", "running"]
        ).exists()
        context["start_form"] = StartAnalysisForm(
            initial={"start_key": uuid4()}, external=context["external_mode"]
        )
        return context


class RequestAnalyzeView(TeamAdminRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        get_object_or_404(
            ClientRequest,
            pk=kwargs["request_id"],
            scope_version__project_id=kwargs["pk"],
            scope_version__project__team=self.team,
            scope_version__project__deletion_requested_at__isnull=True,
        )
        form = StartAnalysisForm(
            request.POST, external=settings.SCOPE_ANALYSIS_BACKEND == "yandex"
        )
        if not form.is_valid():
            return HttpResponse(
                "Проверьте ключ запуска и подтверждение внешней обработки.", status=400
            )
        try:
            start_analysis(
                request.user,
                self.team.pk,
                kwargs["pk"],
                kwargs["request_id"],
                **form.cleaned_data
            )
        except ValidationError as exc:
            status = (
                429
                if isinstance(exc, AnalysisQuota)
                else 409 if isinstance(exc, AnalysisConflict) else 400
            )
            # Only service-defined messages, never submitted document content.
            return HttpResponse(
                " ".join(exc.messages),
                status=status,
                content_type="text/plain; charset=utf-8",
            )
        return redirect(
            "request_detail",
            team_id=self.team.pk,
            pk=kwargs["pk"],
            request_id=kwargs["request_id"],
        )
