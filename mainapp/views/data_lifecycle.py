from uuid import uuid4
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import FormView, TemplateView
from mainapp.forms.data_lifecycle import ProjectDeletionForm
from mainapp.models.scope_control import Project
from mainapp.services.data_lifecycle import (
    _owner,
    preview_deletion,
    request_deletion,
    deletion_status,
    DeletionConflict,
)
from mainapp.views.teams import TeamViewMixin


class OwnerViewMixin(TeamViewMixin):
    def dispatch(self, request, *args, **kwargs):
        # TeamViewMixin resolves tenant/login first; services also recheck on write.
        return super().dispatch(request, *args, **kwargs)

    def owner(self):
        return _owner(self.request.user, self.team.pk)


class ProjectDeleteView(OwnerViewMixin, FormView):
    form_class = ProjectDeletionForm
    template_name = "mainapp/scope_control/project_delete.html"

    def get_initial(self):
        self.owner()
        return {"submission_key": uuid4()}

    def get_context_data(self, **kwargs):
        self.owner()
        return {
            **super().get_context_data(**kwargs),
            "project": get_object_or_404(Project, pk=self.kwargs["pk"], team=self.team),
        }

    def form_valid(self, form):
        self.owner()
        data = form.cleaned_data.copy()
        try:
            if self.request.POST.get("action") == "preview":
                token = preview_deletion(
                    self.request.user,
                    self.team.pk,
                    self.kwargs["pk"],
                    name=data["name"],
                )
                values = {**data, "preview_token": token, "confirmed": False}
                return self.render_to_response(
                    self.get_context_data(
                        form=self.form_class(initial=values), preview=True
                    )
                )
            receipt = request_deletion(
                self.request.user, self.team.pk, self.kwargs["pk"], **data
            )
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.render_to_response(
                self.get_context_data(form=form),
                status=409 if isinstance(exc, DeletionConflict) else 400,
            )
        return redirect(
            "project_deletion_status", team_id=self.team.pk, receipt_id=receipt.pk
        )

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form), status=400)


class ProjectDeletionStatusView(OwnerViewMixin, TemplateView):
    template_name = "mainapp/scope_control/project_deletion_status.html"

    def get_context_data(self, **kwargs):
        return {
            **super().get_context_data(**kwargs),
            "receipt": deletion_status(
                self.request.user, self.team.pk, self.kwargs["receipt_id"]
            ),
        }
