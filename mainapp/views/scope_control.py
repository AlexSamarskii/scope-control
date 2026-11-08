from django.core.paginator import Paginator
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import DetailView, FormView, ListView, TemplateView

from mainapp.forms.scope_control import ProjectForm, ScopeConfirmationForm
from mainapp.models import get_default_team_for_user
from mainapp.models.scope_control import Project
from mainapp.services.scope_control import (
    StaleScopeVersion,
    confirm_scope,
    create_project,
    rename_project,
)
from mainapp.views.teams import TeamAdminRequiredMixin, TeamViewMixin
from mainapp.services.project_workflow import workflow_for, describe_requests


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "mainapp/dashboard/main.html"

    def get(self, request, *args, **kwargs):
        team = get_default_team_for_user(request.user)
        if team:
            return redirect("project_list", team_id=team.pk)
        token = request.session.get("scope_invitation_token")
        if token:
            return redirect("accept_invitation", token=token)
        return super().get(request, *args, **kwargs)


class ProjectListView(TeamViewMixin, TemplateView):
    template_name = "mainapp/scope_control/project_list.html"

    def get_context_data(self, **kwargs):
        from mainapp.views.workspace import workspace_context

        return {
            **super().get_context_data(**kwargs),
            **workspace_context(self.request, self.team, self.team_membership.role),
        }


class ProjectCreateView(TeamAdminRequiredMixin, FormView):
    template_name = "mainapp/scope_control/project_form.html"
    form_class = ProjectForm

    def form_valid(self, form):
        project = create_project(self.request.user, self.team.pk, **form.cleaned_data)
        return redirect("project_detail", team_id=self.team.pk, pk=project.pk)


class ProjectDetailView(TeamViewMixin, DetailView):
    template_name = "mainapp/scope_control/project_detail.html"
    context_object_name = "project"

    def get_queryset(self):
        return Project.objects.filter(team=self.team)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        versions = self.object.versions.select_related("confirmed_by")
        version_id = self.kwargs.get("version_id")
        context["selected_version"] = (
            get_object_or_404(versions, pk=version_id)
            if version_id
            else versions.first()
        )
        context["versions"] = versions.defer("body", "exclusions", "revision_rules")
        context["historical_view"] = bool(version_id)
        context["workflow"] = workflow_for(
            self.request.user,
            self.team.pk,
            self.object.pk,
            self.request.GET.get("focus"),
        )
        context["latest_version"] = context["workflow"]["latest_version"]
        from mainapp.models.analysis import ClientRequest

        context["client_requests"] = Paginator(
            ClientRequest.objects.filter(
                scope_version__project=self.object
            ).select_related("scope_version"),
            25,
        ).get_page(self.request.GET.get("requests_page"))
        page = context["client_requests"]
        page.object_list = describe_requests(
            self.request.user, self.team.pk, self.object.pk, list(page.object_list)
        )
        focus = context["workflow"]["request"]
        from urllib.parse import urlencode

        params = {"focus": str(focus.pk)} if focus else {}
        for prefix, other_page in (
            ("request_page_prefix", "changes_page"),
            ("change_page_prefix", "requests_page"),
        ):
            query = dict(params)
            if self.request.GET.get(other_page):
                query[other_page] = self.request.GET[other_page]
            context[prefix] = "?" + (urlencode(query) + "&" if query else "")
        from mainapp.services.change_exports import current_cards, snapshot_state
        from mainapp.models.change_control import CATEGORY_LABELS, STATE_LABELS

        context["change_cards"] = Paginator(
            current_cards(self.request.user, self.team.pk, self.object.pk), 25
        ).get_page(self.request.GET.get("changes_page"))
        for card in context["change_cards"]:
            card.category_label = CATEGORY_LABELS[card.snapshot["category"]]
            card.state_label = STATE_LABELS[snapshot_state(card.snapshot)]
        return context


class ProjectRenameView(TeamAdminRequiredMixin, FormView):
    template_name = "mainapp/scope_control/project_form.html"
    form_class = ProjectForm

    def get_project(self):
        return get_object_or_404(Project, pk=self.kwargs["pk"], team=self.team)

    def get_initial(self):
        return {"name": self.get_project().name}

    def form_valid(self, form):
        project = rename_project(
            self.request.user, self.team.pk, self.kwargs["pk"], **form.cleaned_data
        )
        return redirect("project_detail", team_id=self.team.pk, pk=project.pk)


class ScopeConfirmView(TeamAdminRequiredMixin, FormView):
    template_name = "mainapp/scope_control/scope_confirm.html"
    form_class = ScopeConfirmationForm

    def get_project(self):
        return get_object_or_404(Project, pk=self.kwargs["pk"], team=self.team)

    def get_initial(self):
        version = self.get_project().versions.first()
        if version is None:
            return {}
        return {
            "body": version.body,
            "exclusions": version.exclusions,
            "revision_rules": version.revision_rules,
            "expected_version": version.pk,
        }

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if (
            self.request.method == "POST"
            and self.request.POST.get("action") == "preview"
        ):
            form.fields["confirmed"].required = False
        return form

    def get_context_data(self, **kwargs):
        project = self.get_project()
        return {
            **super().get_context_data(**kwargs),
            "project": project,
            "workflow": workflow_for(
                self.request.user,
                self.team.pk,
                project.pk,
                select_latest=False,
                form_step=1,
            ),
        }

    def form_valid(self, form):
        if self.request.POST.get("action") == "preview":
            return self.render_to_response(
                self.get_context_data(form=form, preview=form.cleaned_data)
            )
        try:
            version = confirm_scope(
                self.request.user, self.team.pk, self.kwargs["pk"], **form.cleaned_data
            )
        except StaleScopeVersion as exc:
            form.add_error(None, exc)
            return self.render_to_response(self.get_context_data(form=form), status=409)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"Подтверждена версия {version.number}.")
        return redirect(
            "scope_version",
            team_id=self.team.pk,
            pk=version.project_id,
            version_id=version.pk,
        )
