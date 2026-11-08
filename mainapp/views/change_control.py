from mainapp.services.scope_diff import scope_diff
from uuid import uuid4
import logging

from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import FormView, TemplateView, View

from mainapp.forms.change_control import DecisionForm, ChangeEventForm, ChangeScopeForm
from mainapp.models.analysis import ClientRequest
from mainapp.services.change_control import (
    ChangeConflict,
    get_card,
    get_project,
    record_decision,
    revise_decision,
    record_shared,
    record_client_response,
    apply_scope_change,
    preview_scope_change,
)
from mainapp.services.change_exports import build_draft, draft_text, csv_text
from mainapp.views.teams import TeamViewMixin, TeamAdminRequiredMixin
from mainapp.services.project_workflow import workflow_for


def detail_redirect(team_id, project_id, card_id):
    return redirect("change_detail", team_id=team_id, pk=project_id, card_id=card_id)


class ChangePrivacyBoundary:
    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except (Http404, PermissionDenied):
            raise
        except Exception:
            # Exception messages (including database errors) may contain private
            # decisions/evidence. Emit only a safe operation code and UUID.
            logging.getLogger(__name__).warning(
                "scope_change_internal_error",
                extra={"project_id": str(kwargs.get("pk", ""))},
            )
            return HttpResponse(
                "Действие не выполнено из-за внутренней ошибки. Повторите попытку; сохранённая история доступна в карточке.",
                status=500,
            )


class CardContext:
    workflow_form_step = None

    def get_card(self):
        return get_card(
            self.request.user, self.team.pk, self.kwargs["pk"], self.kwargs["card_id"]
        )

    def get_context_data(self, **kwargs):
        card = self.get_card()
        current = card.current_revision
        event = current.last_event
        return {
            **super().get_context_data(**kwargs),
            "card": card,
            "project": card.client_request.scope_version.project,
            "current": current,
            "current_event": event,
            "workflow": workflow_for(
                self.request.user,
                self.team.pk,
                self.kwargs["pk"],
                card.client_request_id,
                form_step=self.workflow_form_step,
            ),
            "state": (
                event.kind
                if event
                else ("draft" if current.category == "possible_change" else "reviewed")
            ),
        }


class SafeChangeForm:
    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form), status=400)

    def failed(self, form, exc):
        # Do not reflect exception repr, SQL, source text or arbitrary IDs.
        form.add_error(
            None,
            "Действие не выполнено. Проверьте поля, актуальную редакцию и исходную версию объёма.",
        )
        return self.render_to_response(
            self.get_context_data(form=form),
            status=409 if isinstance(exc, ChangeConflict) else 400,
        )


class ChangeDecisionView(
    ChangePrivacyBoundary, TeamAdminRequiredMixin, SafeChangeForm, FormView
):
    template_name = "mainapp/scope_control/change_revision_form.html"
    form_class = DecisionForm

    def get_item(self):
        get_project(self.request.user, self.team.pk, self.kwargs["pk"])
        return get_object_or_404(
            ClientRequest,
            pk=self.kwargs["request_id"],
            scope_version__project_id=self.kwargs["pk"],
            scope_version__project__team=self.team,
        )

    def get_initial(self):
        return {"submission_key": uuid4()}

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "item": self.get_item()}

    def get_context_data(self, **kwargs):
        item = self.get_item()
        return {
            **super().get_context_data(**kwargs),
            "client_request": item,
            "project": item.scope_version.project,
            "workflow": workflow_for(
                self.request.user, self.team.pk, self.kwargs["pk"], item.pk, form_step=3
            ),
        }

    def form_valid(self, form):
        data = form.cleaned_data.copy()
        run = data.pop("analysis_run")
        data["analysis_run_id"] = run.pk if run else None
        data.pop("expected_revision_id")
        data.pop("expected_event_id")
        try:
            revision = record_decision(
                self.request.user,
                self.team.pk,
                self.kwargs["pk"],
                self.kwargs["request_id"],
                **data,
            )
        except ValidationError as exc:
            return self.failed(form, exc)
        return detail_redirect(self.team.pk, self.kwargs["pk"], revision.card_id)


class ChangeReviseView(CardContext, ChangeDecisionView):
    workflow_form_step = 3

    def get_item(self):
        return self.get_card().client_request

    def get_initial(self):
        revision = self.get_card().current_revision
        event = revision.last_event
        return {
            **{
                f: getattr(revision, f)
                for f in (
                    "category",
                    "description",
                    "rationale",
                    "amount",
                    "currency",
                    "deadline_effect",
                    "analysis_run_id",
                )
            },
            "analysis_run": revision.analysis_run_id,
            "submission_key": uuid4(),
            "expected_revision_id": revision.pk,
            "expected_event_id": event.pk if event else None,
        }

    def form_valid(self, form):
        data = form.cleaned_data.copy()
        run = data.pop("analysis_run")
        data["analysis_run_id"] = run.pk if run else None
        try:
            revision = revise_decision(
                self.request.user,
                self.team.pk,
                self.kwargs["pk"],
                self.kwargs["card_id"],
                **data,
            )
        except ValidationError as exc:
            return self.failed(form, exc)
        return detail_redirect(self.team.pk, self.kwargs["pk"], revision.card_id)


class ChangeDetailView(ChangePrivacyBoundary, TeamViewMixin, CardContext, TemplateView):
    template_name = "mainapp/scope_control/change_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        card = context["card"]
        context["draft"] = build_draft(context["current"])
        context["revisions"] = Paginator(
            card.revisions.select_related("created_by", "analysis_run"), 25
        ).get_page(self.request.GET.get("revisions_page"))
        context["events"] = Paginator(
            card.events.select_related(
                "created_by", "revision", "resulting_scope_version"
            ).order_by("-revision__number", "-number"),
            25,
        ).get_page(self.request.GET.get("events_page"))
        context["stale_source"] = (
            card.client_request.scope_version.project.versions.first().pk
            != card.client_request.scope_version_id
        )
        return context


class ChangeEventView(
    ChangePrivacyBoundary, TeamAdminRequiredMixin, CardContext, SafeChangeForm, FormView
):
    template_name = "mainapp/scope_control/change_event_form.html"
    form_class = ChangeEventForm
    workflow_form_step = 4

    def post(self, request, *args, **kwargs):
        with timezone.override(self.team.tzinfo()):
            return super().post(request, *args, **kwargs)

    def get_initial(self):
        revision = self.get_card().current_revision
        event = revision.last_event
        return {
            "submission_key": uuid4(),
            "expected_revision_id": revision.pk,
            "expected_event_id": event.pk if event else None,
        }

    def form_valid(self, form):
        data = form.cleaned_data.copy()
        try:
            if data["kind"] == "shared":
                data.pop("kind")
                for field in (
                    "sender_name",
                    "occurred_at",
                    "evidence_text",
                    "source_description",
                ):
                    data.pop(field)
                record_shared(
                    self.request.user,
                    self.team.pk,
                    self.kwargs["pk"],
                    self.kwargs["card_id"],
                    **data,
                )
            else:
                record_client_response(
                    self.request.user,
                    self.team.pk,
                    self.kwargs["pk"],
                    self.kwargs["card_id"],
                    **data,
                )
        except ValidationError as exc:
            return self.failed(form, exc)
        return detail_redirect(self.team.pk, self.kwargs["pk"], self.kwargs["card_id"])


class ChangeApplyView(
    ChangePrivacyBoundary, TeamAdminRequiredMixin, CardContext, SafeChangeForm, FormView
):
    template_name = "mainapp/scope_control/change_scope_confirm.html"
    form_class = ChangeScopeForm
    workflow_form_step = 4

    def get_initial(self):
        card = self.get_card()
        revision = card.current_revision
        event = revision.last_event
        # Copy existing conditions only; the person edits and explicitly confirms the change.
        return {
            "body": card.client_request.scope_version.body,
            "exclusions": card.client_request.scope_version.exclusions,
            "revision_rules": card.client_request.scope_version.revision_rules,
            "submission_key": uuid4(),
            "expected_revision_id": revision.pk,
            "expected_event_id": event.pk if event else None,
            "expected_version": card.client_request.scope_version_id,
        }

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if (
            self.request.method == "POST"
            and self.request.POST.get("action") == "preview"
        ):
            form.fields["confirmed"].required = False
            form.data = form.data.copy()
            form.data["confirmed"] = ""
        return form

    def form_valid(self, form):
        data = form.cleaned_data.copy()
        action = self.request.POST.get("action")
        try:
            if action == "preview":
                data.pop("confirmed")
                data.pop("preview_token")
                token = preview_scope_change(
                    self.request.user,
                    self.team.pk,
                    self.kwargs["pk"],
                    self.kwargs["card_id"],
                    **data,
                )
                form.data = form.data.copy()
                form.data["preview_token"] = token
                return self.render_to_response(
                    self.get_context_data(form=form, preview=data, scope_diff=scope_diff(self.get_card().client_request.scope_version, data))
                )
            if action != "confirm":
                raise ValidationError("Выберите предпросмотр или подтверждение.")
            apply_scope_change(
                self.request.user,
                self.team.pk,
                self.kwargs["pk"],
                self.kwargs["card_id"],
                **data,
            )
        except ValidationError as exc:
            return self.failed(form, exc)
        return detail_redirect(self.team.pk, self.kwargs["pk"], self.kwargs["card_id"])


def attachment(text, filename, content_type):
    response = HttpResponse(text, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    return response


class ChangeDraftView(ChangePrivacyBoundary, TeamViewMixin, View):
    def get(self, request, **kwargs):
        try:
            text = draft_text(
                request.user,
                self.team.pk,
                kwargs["pk"],
                kwargs["card_id"],
                kwargs["revision_id"],
            )
        except ChangeConflict:
            return HttpResponse(
                "Редакция устарела. Откройте актуальную карточку.", status=409
            )
        return attachment(
            text, f'change-{kwargs["card_id"]}.txt', "text/plain; charset=utf-8"
        )


class ChangeExportView(ChangePrivacyBoundary, TeamViewMixin, View):
    def get(self, request, **kwargs):
        try:
            text = csv_text(request.user, self.team.pk, kwargs["pk"])
        except ValidationError:
            return HttpResponse(
                "Выгрузка ограничена 1000 карточками проекта. Частичный реестр не сформирован.",
                status=400,
            )
        return attachment(
            text, f'changes-{kwargs["pk"]}.csv', "text/csv; charset=utf-8"
        )
