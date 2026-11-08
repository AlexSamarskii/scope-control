from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import Client, RequestFactory
from django.utils import timezone

from mainapp.models import TeamMembership
from mainapp.models.change_control import ChangeCard, ChangeRevision, ChangeEvent
from mainapp.models.scope_control import ScopeVersion
from mainapp.services.change_control import (
    get_card,
    get_project,
    record_decision,
    record_shared,
)
from mainapp.services.change_exports import csv_text, draft_text
from mainapp.tests.test_scope_change_control import ChangeBase


class ChangeAccessTests(ChangeBase):
    def decision_post(self):
        return {
            "category": "possible_change",
            "description": "Добавить каталог.",
            "rationale": "Исключён из объёма.",
            "amount": "1500.00",
            "currency": "RUB",
            "deadline_effect": "Два дня",
            "submission_key": uuid4(),
            "confirmed": "on",
        }

    def test_form_requires_explicit_human_inputs_and_links_to_card(self):
        self.client.force_login(self.owner)
        url = self.url("change_create", request_id=self.item.pk)
        form = self.client.get(url).context["form"]
        self.assertFalse(form["confirmed"].value())
        self.assertIsNone(form["category"].value())
        data = self.decision_post()
        data.pop("confirmed")
        self.assertEqual(self.client.post(url, data).status_code, 400)
        data["confirmed"] = "on"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        revision = ChangeRevision.objects.get()
        detail = self.client.get(self.url("change_detail", card_id=revision.card_id))
        self.assertContains(detail, "Решение менеджера")
        self.assertContains(
            self.client.get(self.url("request_detail", request_id=self.item.pk)),
            "Открыть решение менеджера",
        )
        self.assertContains(
            self.client.get(self.url("project_detail")), "Текущие редакции карточек"
        )
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(ChangeCard.objects.count(), 1)

    def test_real_http_response_preview_confirmation_and_repeat(self):
        revision = self.decision()
        self.client.force_login(self.owner)
        event_url = self.url("change_event", card_id=revision.card_id)
        shared = {
            "kind": "shared",
            "submission_key": uuid4(),
            "expected_revision_id": revision.pk,
            "confirmed": "on",
        }
        self.assertEqual(self.client.post(event_url, shared).status_code, 302)
        self.team.timezone = "Europe/Moscow"
        self.team.save()
        when = (timezone.now() - timedelta(minutes=5)).astimezone(self.team.tzinfo())
        data = {
            "kind": "accepted",
            "sender_name": "Клиент",
            "occurred_at": when.strftime("%Y-%m-%dT%H:%M"),
            "evidence_text": "Согласовано вручную.",
            "submission_key": uuid4(),
            "expected_revision_id": revision.pk,
            "expected_event_id": revision.last_event.pk,
            "confirmed": "on",
        }
        self.assertEqual(self.client.post(event_url, data).status_code, 302)
        answer = revision.last_event
        self.assertEqual(answer.occurred_at, when.replace(second=0, microsecond=0))
        url = self.url("change_apply", card_id=revision.card_id)
        initial = self.client.get(url).context["form"]
        self.assertFalse(initial["confirmed"].value())
        # Quality fix: copy the exact source conditions without applying the proposed work.
        self.assertEqual(initial["body"].value(), self.version.body)
        self.assertEqual(initial["exclusions"].value(), self.version.exclusions)
        self.assertEqual(initial["revision_rules"].value(), self.version.revision_rules)
        payload = {
            **self.apply_data(revision, answer),
            "action": "preview",
            "confirmed": "on",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 200)
        token = response.context["form"]["preview_token"].value()
        self.assertTrue(token)
        self.assertFalse(response.context["form"]["confirmed"].value())
        self.assertEqual(ScopeVersion.objects.count(), 1)
        payload.update(action="confirm", preview_token=token, confirmed="on")
        self.assertEqual(
            self.client.post(
                url, {**payload, "body": "Подменённый после preview"}
            ).status_code,
            400,
        )
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(ScopeVersion.objects.count(), 2)
        self.assertEqual(ChangeEvent.objects.filter(kind="applied").count(), 1)

    def test_every_get_is_readonly_and_mutations_do_not_accept_get(self):
        revision = self.decision()
        self.client.force_login(self.owner)
        before = (
            ChangeCard.objects.count(),
            ChangeRevision.objects.count(),
            ChangeEvent.objects.count(),
            ScopeVersion.objects.count(),
        )
        urls = [
            self.url("change_create", request_id=self.item.pk),
            *[
                self.url(n, card_id=revision.card_id)
                for n in [
                    "change_detail",
                    "change_revise",
                    "change_event",
                    "change_apply",
                ]
            ],
            self.url("change_draft", card_id=revision.card_id, revision_id=revision.pk),
            self.url("change_export"),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(
            before,
            (
                ChangeCard.objects.count(),
                ChangeRevision.objects.count(),
                ChangeEvent.objects.count(),
                ScopeVersion.objects.count(),
            ),
        )

    def test_all_endpoints_and_services_reject_foreign_tenant_ids(self):
        revision = self.decision()
        self.client.force_login(self.outsider)
        urls = [
            self.url("change_create", request_id=self.item.pk),
            *[
                self.url(n, card_id=revision.card_id)
                for n in [
                    "change_detail",
                    "change_revise",
                    "change_event",
                    "change_apply",
                ]
            ],
            self.url("change_draft", card_id=revision.card_id, revision_id=revision.pk),
            self.url("change_export"),
        ]
        for url in urls:
            for method in (self.client.get, self.client.post):
                with self.subTest(url=url, method=method.__name__):
                    self.assertEqual(method(url, {}).status_code, 404)
        for action in (
            lambda: get_card(
                self.outsider, self.team.pk, self.project.pk, revision.card_id
            ),
            lambda: csv_text(self.outsider, self.team.pk, self.project.pk),
            lambda: draft_text(
                self.outsider,
                self.team.pk,
                self.project.pk,
                revision.card_id,
                revision.pk,
            ),
            lambda: record_shared(
                self.outsider,
                self.team.pk,
                self.project.pk,
                revision.card_id,
                expected_revision_id=revision.pk,
                submission_key=uuid4(),
                confirmed=True,
            ),
        ):
            with self.assertRaises(Http404):
                action()
        self.assertFalse(ChangeEvent.objects.exists())

    def test_mismatched_project_card_and_optional_analysis_ids_not_authority(self):
        revision = self.decision()
        self.client.force_login(self.owner)
        url = self.url(
            "change_detail", pk=self.other_project.pk, card_id=revision.card_id
        )
        self.assertEqual(self.client.get(url).status_code, 404)
        data = self.decision_post()
        data["analysis_run"] = uuid4()
        self.assertEqual(
            self.client.post(
                self.url("change_create", request_id=self.item.pk), data
            ).status_code,
            400,
        )
        with self.assertRaises(Http404):
            record_decision(
                self.owner,
                self.team.pk,
                self.other_project.pk,
                self.item.pk,
                category="included",
                description="a",
                rationale="b",
                submission_key=uuid4(),
                confirmed=True,
            )
        self.assertEqual(ChangeCard.objects.count(), 1)

    def test_member_and_viewer_read_export_but_cannot_write(self):
        revision = self.decision()
        self.client.force_login(self.owner)
        for role in ("member", "viewer"):
            TeamMembership.objects.filter(pk=self.membership.pk).update(role=role)
            self.assertEqual(
                self.client.get(
                    self.url("change_detail", card_id=revision.card_id)
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.get(self.url("change_export")).status_code, 200
            )
            for name in ("change_revise", "change_event", "change_apply"):
                self.assertEqual(
                    self.client.post(
                        self.url(name, card_id=revision.card_id), {}
                    ).status_code,
                    403,
                )
            with self.assertRaises(PermissionDenied):
                record_shared(
                    self.owner,
                    self.team.pk,
                    self.project.pk,
                    revision.card_id,
                    expected_revision_id=revision.pk,
                    submission_key=uuid4(),
                    confirmed=True,
                )
        self.assertFalse(ChangeEvent.objects.exists())

    def test_expired_revoked_and_inactive_access_rechecked_by_service(self):
        revision = self.decision()
        TeamMembership.objects.filter(pk=self.membership.pk).update(
            access_expires_at=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaises(Http404):
            csv_text(self.owner, self.team.pk, self.project.pk)
        with self.assertRaises(Http404):
            self.shared(revision)
        TeamMembership.objects.filter(pk=self.membership.pk).update(
            access_expires_at=None
        )
        self.owner.is_active = False
        self.owner.save()
        with self.assertRaises(Http404):
            self.shared(revision)
        self.owner.is_active = True
        self.owner.save()
        self.team.is_active = False
        self.team.save()
        with self.assertRaises(Http404):
            self.shared(revision)
        self.team.is_active = True
        self.team.save()
        self.membership.delete()
        with self.assertRaises(Http404):
            draft_text(
                self.owner, self.team.pk, self.project.pk, revision.card_id, revision.pk
            )

    def test_csrf_and_readonly_admin_do_not_bypass_guards(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(
                self.url("change_create", request_id=self.item.pk), self.decision_post()
            ).status_code,
            403,
        )
        revision, event = self.accepted()
        for model, record in (
            (ChangeCard, revision.card),
            (ChangeRevision, revision),
            (ChangeEvent, event),
        ):
            handler = admin.site._registry[model]
            for user, visible in ((self.owner, True), (self.outsider, False)):
                request = RequestFactory().get("/admin/")
                request.user = user
                self.assertEqual(
                    handler.get_queryset(request).filter(pk=record.pk).exists(), visible
                )
                self.assertEqual(handler.has_view_permission(request, record), visible)
                self.assertFalse(handler.has_add_permission(request))
                self.assertFalse(handler.has_change_permission(request, record))
                self.assertFalse(handler.has_delete_permission(request, record))
