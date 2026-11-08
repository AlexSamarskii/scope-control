from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import FormView

from mainapp.models.scope_control import Project
from mainapp.services.demo_project import create_demo_project
from mainapp.services.workspace import (
    project_query,
    request_query,
    describe_queue,
    financial_summary,
    set_archived,
    QUEUE_FILTERS,
)
from mainapp.views.teams import TeamAdminRequiredMixin


class WorkspaceFilters(forms.Form):
    q = forms.CharField(required=False, max_length=200, label="Поиск")
    archive = forms.ChoiceField(
        required=False,
        choices=[("active", "Активные"), ("archived", "Архив"), ("all", "Все проекты")],
    )
    state = forms.ChoiceField(required=False, choices=QUEUE_FILTERS)


def workspace_context(request, team, role):
    form = WorkspaceFilters(request.GET)
    valid = form.is_valid()
    data = form.cleaned_data if valid else {}
    query = data.get("q", "")
    archive = data.get("archive") or "active"
    state = data.get("state") or ("all" if query else "open")
    projects = project_query(request.user, team.pk, archive=archive, query=query)
    items = request_query(
        request.user, team.pk, archive=archive, query=query, state=state
    )
    if not valid:
        projects, items = projects.none(), items.none()
    project_page = Paginator(projects, 12).get_page(request.GET.get("projects_page"))
    queue_page = Paginator(items, 20).get_page(request.GET.get("queue_page"))
    queue_page.object_list = describe_queue(
        list(queue_page.object_list), team.pk, can_write=role in ("owner", "admin")
    )
    params = {"q": query, "archive": archive, "state": state}
    return dict(
        search_form=form,
        query=query,
        archive=archive,
        queue_state=state,
        queue_filters=QUEUE_FILTERS,
        projects=project_page,
        queue=queue_page,
        project_page_prefix="?"
        + urlencode({**params, "queue_page": queue_page.number})
        + "&",
        queue_page_prefix="?"
        + urlencode({**params, "projects_page": project_page.number})
        + "&",
        finance=financial_summary(request.user, team.pk),
        demo_project=Project.objects.filter(team=team, is_demo=True).first(),
    )


class ConfirmationForm(forms.Form):
    confirmed = forms.BooleanField(label="Подтверждаю действие")


class ArchiveForm(ConfirmationForm):
    action = forms.ChoiceField(
        choices=[("archive", "Архивировать"), ("restore", "Вернуть в работу")],
        widget=forms.HiddenInput,
    )


class ProjectArchiveView(TeamAdminRequiredMixin, FormView):
    template_name = "mainapp/scope_control/project_archive.html"
    form_class = ArchiveForm

    def get_project(self):
        return get_object_or_404(Project, pk=self.kwargs["pk"], team=self.team)

    def get_initial(self):
        return {"action": "restore" if self.get_project().archived_at else "archive"}

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "project": self.get_project()}

    def form_valid(self, form):
        project = set_archived(
            self.request.user,
            self.team.pk,
            self.kwargs["pk"],
            archived=form.cleaned_data["action"] == "archive",
            confirmed=form.cleaned_data["confirmed"],
        )
        messages.success(
            self.request,
            (
                "Проект в архиве. История сохранена."
                if project.archived_at
                else "Проект возвращён в работу."
            ),
        )
        return redirect("project_detail", team_id=self.team.pk, pk=project.pk)


class DemoProjectView(TeamAdminRequiredMixin, FormView):
    template_name = "mainapp/scope_control/demo_project.html"
    form_class = ConfirmationForm

    def form_valid(self, form):
        project = create_demo_project(
            self.request.user, self.team.pk, confirmed=form.cleaned_data["confirmed"]
        )
        return redirect("project_detail", team_id=self.team.pk, pk=project.pk)
