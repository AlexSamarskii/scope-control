from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.core import signing
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from mainapp.models.analysis import _domain_write, AnalysisRun
from mainapp.models.change_control import ChangeCard, ChangeRevision, ChangeEvent
from mainapp.models.scope_control import ScopeVersion
from mainapp.services.change_control import (
    ChangeConflict,
    record_decision,
    revise_decision,
    record_shared,
    record_client_response,
    preview_scope_change,
    apply_scope_change,
)
from mainapp.services.analysis import execute_run
from mainapp.services.scope_control import confirm_scope
from mainapp.tests.test_scope_analysis import AnalysisBase


@override_settings(SCOPE_ANALYSIS_BACKEND="disabled")
class ChangeBase(AnalysisBase):
    def decision(self, item=None, **kwargs):
        return record_decision(
            self.owner,
            self.team.pk,
            self.project.pk,
            (item or self.item).pk,
            **{
                "category": "possible_change",
                "description": "Добавить каталог товаров.",
                "rationale": "Каталог исключён из исходного объёма.",
                "amount": "1500.00",
                "currency": "RUB",
                "deadline_effect": "Дополнительно два рабочих дня.",
                "submission_key": uuid4(),
                "confirmed": True,
                **kwargs,
            }
        )

    def revise(self, revision, **kwargs):
        last = revision.last_event
        return revise_decision(
            self.owner,
            self.team.pk,
            self.project.pk,
            revision.card_id,
            **{
                "expected_revision_id": revision.pk,
                "expected_event_id": last.pk if last else None,
                "category": revision.category,
                "description": revision.description,
                "rationale": revision.rationale,
                "amount": revision.amount,
                "currency": revision.currency,
                "deadline_effect": revision.deadline_effect,
                "submission_key": uuid4(),
                "confirmed": True,
                **kwargs,
            }
        )

    def shared(self, revision, **kwargs):
        last = revision.last_event
        return record_shared(
            self.owner,
            self.team.pk,
            self.project.pk,
            revision.card_id,
            **{
                "expected_revision_id": revision.pk,
                "expected_event_id": last.pk if last else None,
                "submission_key": uuid4(),
                "confirmed": True,
                **kwargs,
            }
        )

    def response(self, revision, **kwargs):
        last = revision.last_event
        return record_client_response(
            self.owner,
            self.team.pk,
            self.project.pk,
            revision.card_id,
            **{
                "kind": "accepted",
                "sender_name": "Синтетический клиент",
                "occurred_at": timezone.now() - timedelta(minutes=1),
                "evidence_text": "Подтверждаю 1500 RUB и два рабочих дня.",
                "source_description": "https://example.invalid/не-загружать",
                "expected_revision_id": revision.pk,
                "expected_event_id": last.pk if last else None,
                "submission_key": uuid4(),
                "confirmed": True,
                **kwargs,
            }
        )

    def accepted(self):
        revision = self.decision()
        self.shared(revision)
        return revision, self.response(revision)

    def apply_data(self, revision, event, **kwargs):
        return {
            "expected_revision_id": revision.pk,
            "expected_event_id": event.pk,
            "expected_version": self.version.pk,
            "submission_key": uuid4(),
            "body": "Объём 😀: форма обратной связи и каталог.",
            "exclusions": "Без оплаты.",
            "revision_rules": "Одна итерация.",
            **kwargs,
        }

    def preview(self, data, card_id):
        return preview_scope_change(
            self.owner, self.team.pk, self.project.pk, card_id, **data
        )

    def apply(self, revision, event, **kwargs):
        data = self.apply_data(revision, event, **kwargs)
        token = self.preview(data, revision.card_id)
        return apply_scope_change(
            self.owner,
            self.team.pk,
            self.project.pk,
            revision.card_id,
            **data,
            preview_token=token,
            confirmed=True
        )


class DecisionTests(ChangeBase):
    def test_manual_disabled_flow_does_not_call_worker_or_model(self):
        with patch("mainapp.services.analysis.publish_run") as publish, patch(
            "mainapp.services.analysis_provider.request_http"
        ) as network:
            revision = self.decision()
        publish.assert_not_called()
        network.assert_not_called()
        self.assertEqual(revision.card.client_request.scope_version_id, self.version.pk)
        self.assertIsNone(revision.analysis_run_id)
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertEqual(revision.state, "draft")

    def test_explicit_category_reason_and_confirmation_required_atomic_create(self):
        for values in (
            {"confirmed": False},
            {"category": ""},
            {"description": "  "},
            {"rationale": ""},
            {"description": "x" * 10001},
        ):
            with self.subTest(values=list(values)), self.assertRaises(ValidationError):
                self.decision(**values)
            self.assertEqual(ChangeCard.objects.count(), 0)
        self.decision()
        self.assertEqual(ChangeCard.objects.count(), 1)

    def test_categories_do_not_invent_price_or_allow_approval(self):
        for category in ("included", "insufficient_evidence"):
            with self.subTest(category=category):
                with self.assertRaises(ValidationError):
                    self.decision(category=category)
                revision = self.decision(
                    item=self.make_request(),
                    category=category,
                    amount=None,
                    currency="",
                    deadline_effect="",
                )
                self.assertEqual(revision.state, "reviewed")
                with self.assertRaises(ValidationError):
                    self.shared(revision)
        self.assertFalse(ChangeEvent.objects.exists())

    def test_money_boundaries_precision_unknown_and_zero(self):
        for value in ("NaN", "Infinity", "-0.01", "1.001", "10000000000", True):
            with self.subTest(value=str(value)), self.assertRaises(ValidationError):
                self.decision(amount=value)
        for values in ({"currency": "GBP"}, {"amount": None}, {"currency": ""}):
            with self.assertRaises(ValidationError):
                self.decision(**values)
        unknown = self.decision(amount=None, currency="")
        self.assertEqual(unknown.amount_label, "Не определена")
        with self.assertRaises(ValidationError):
            self.shared(unknown)
        zero = self.revise(unknown, amount="0", currency="RUB")
        self.assertEqual(zero.amount, Decimal("0.00"))
        self.assertTrue(zero.estimate_complete)
        self.shared(zero)
        maximum = self.decision(item=self.make_request(), amount="9999999999.99")
        self.assertEqual(maximum.amount, Decimal("9999999999.99"))

    def test_creation_replay_same_payload_after_revision_and_conflicting_keys(self):
        key = uuid4()
        first = self.decision(submission_key=key, amount="1500")
        self.revise(first)
        self.assertEqual(
            self.decision(submission_key=key, amount="1500.00").pk, first.pk
        )
        with self.assertRaises(ChangeConflict):
            self.decision(submission_key=key, description="Другое")
        with self.assertRaises(ChangeConflict):
            self.decision()
        self.assertEqual(ChangeCard.objects.count(), 1)
        self.assertEqual(ChangeRevision.objects.count(), 2)

    def test_optional_analysis_must_exist_be_validated_and_bound_to_same_request(self):
        with self.assertRaises(ValidationError):
            self.decision(analysis_run_id=uuid4())
        with override_settings(SCOPE_ANALYSIS_BACKEND="fixture"):
            run = self.start()
            with self.assertRaises(ValidationError):
                self.decision(analysis_run_id=run.pk)
            execute_run(run.pk)
        other = self.make_request()
        with self.assertRaises(ValidationError):
            self.decision(item=other, analysis_run_id=run.pk)
        revision = self.decision(analysis_run_id=run.pk)
        self.assertEqual(revision.analysis_run_id, run.pk)

    def test_revision_preserves_accepted_history_and_resets_state(self):
        first, response = self.accepted()
        key = uuid4()
        second = self.revise(first, amount="2000.00", submission_key=key)
        self.assertEqual(second.number, 2)
        self.assertEqual(second.previous_revision_id, first.pk)
        self.assertEqual(second.state, "draft")
        first.refresh_from_db()
        response.refresh_from_db()
        self.assertEqual(first.amount, Decimal("1500.00"))
        self.assertEqual(first.state, "accepted")
        self.assertEqual(
            response.evidence_text, "Подтверждаю 1500 RUB и два рабочих дня."
        )
        self.assertEqual(
            self.revise(first, amount="2000.00", submission_key=key).pk, second.pk
        )
        with self.assertRaises(ChangeConflict):
            self.revise(first, amount="3000.00", submission_key=key)
        with self.assertRaises(ChangeConflict):
            self.revise(first)

    def test_new_event_invalidates_revision_token_and_create_key_cannot_be_reused(self):
        key = uuid4()
        revision = self.decision(submission_key=key)
        self.shared(revision)
        with self.assertRaises(ChangeConflict):
            self.revise(revision, expected_event_id=None)
        with self.assertRaises(ChangeConflict):
            self.revise(revision, submission_key=key)
        self.assertEqual(ChangeRevision.objects.count(), 1)

    def test_guards_block_mutation_bulk_and_deletion(self):
        revision, event = self.accepted()
        for record in (revision.card, revision, event):
            for action in (
                lambda r=record: r.save(),
                lambda r=record: r.delete(),
                lambda r=record: type(r)
                .objects.filter(pk=r.pk)
                .update(created_at=timezone.now()),
                lambda r=record: type(r).objects.bulk_update([r], ["updated_at"]),
            ):
                with self.assertRaises(ValidationError):
                    action()
            with _domain_write(), self.assertRaises(ValidationError):
                record.save()
        self.assertEqual(ChangeEvent.objects.count(), 2)


class ApprovalAndApplyTests(ChangeBase):
    def test_response_requires_shared_current_revision_and_complete_human_estimate(
        self,
    ):
        revision = self.decision()
        with self.assertRaises(ValidationError):
            self.response(revision)
        self.shared(revision)
        second = self.revise(revision)
        with self.assertRaises(ChangeConflict):
            self.response(revision)
        self.assertEqual(second.state, "draft")
        with self.assertRaises(ValidationError):
            self.shared(second, confirmed=False)

    def test_response_requires_sender_evidence_aware_nonfuture_time(self):
        revision = self.decision()
        self.shared(revision)
        for values in (
            {"sender_name": ""},
            {"evidence_text": " "},
            {"occurred_at": None},
            {"occurred_at": timezone.now() + timedelta(days=1)},
            {"occurred_at": timezone.now().replace(tzinfo=None)},
            {"confirmed": False},
            {"source_description": "x" * 2001},
        ):
            with self.subTest(values=list(values)), self.assertRaises(ValidationError):
                self.response(revision, **values)
        self.assertEqual(ChangeEvent.objects.count(), 1)
        self.assertEqual(self.response(revision).kind, "accepted")

    def test_rejection_is_human_terminal_and_cannot_apply(self):
        revision = self.decision()
        self.shared(revision)
        event = self.response(revision, kind="rejected")
        with self.assertRaises(ChangeConflict):
            self.preview(self.apply_data(revision, event), revision.card_id)
        with self.assertRaises(ValidationError):
            self.response(revision)
        self.assertEqual(ScopeVersion.objects.count(), 1)

    def test_event_replay_and_payload_conflict(self):
        revision = self.decision()
        key = uuid4()
        first = self.shared(revision, submission_key=key)
        self.assertEqual(
            self.shared(revision, submission_key=key, expected_event_id=None).pk,
            first.pk,
        )
        with self.assertRaises(ChangeConflict):
            self.response(revision, submission_key=key)
        key = uuid4()
        when = timezone.now() - timedelta(minutes=1)
        result = self.response(revision, submission_key=key, occurred_at=when)
        self.assertEqual(
            self.response(
                revision,
                submission_key=key,
                occurred_at=when,
                expected_event_id=first.pk,
            ).pk,
            result.pk,
        )
        with self.assertRaises(ChangeConflict):
            self.response(
                revision,
                submission_key=key,
                occurred_at=when,
                evidence_text="Другое",
                expected_event_id=first.pk,
            )

    def test_preview_is_readonly_and_receipt_binds_actor_text_and_source(self):
        revision, event = self.accepted()
        data = self.apply_data(revision, event)
        token = self.preview(data, revision.card_id)
        self.assertEqual(ScopeVersion.objects.count(), 1)
        self.assertEqual(ChangeEvent.objects.count(), 2)
        for changed in (
            {"body": "Другой текст"},
            {"preview_token": ""},
            {"confirmed": False},
        ):
            values = {**data, "preview_token": token, "confirmed": True, **changed}
            with self.assertRaises(ValidationError):
                apply_scope_change(
                    self.owner,
                    self.team.pk,
                    self.project.pk,
                    revision.card_id,
                    **values
                )
        with patch(
            "mainapp.services.change_control.signing.loads",
            side_effect=signing.SignatureExpired(),
        ):
            with self.assertRaises(ValidationError):
                apply_scope_change(
                    self.owner,
                    self.team.pk,
                    self.project.pk,
                    revision.card_id,
                    **data,
                    preview_token=token,
                    confirmed=True
                )
        self.assertEqual(ScopeVersion.objects.count(), 1)

    def test_apply_commits_one_version_and_event_and_replays_after_expiry(self):
        revision, event = self.accepted()
        data = self.apply_data(revision, event)
        token = self.preview(data, revision.card_id)
        result = apply_scope_change(
            self.owner,
            self.team.pk,
            self.project.pk,
            revision.card_id,
            **data,
            preview_token=token,
            confirmed=True
        )
        with patch(
            "mainapp.services.change_control.signing.loads",
            side_effect=signing.SignatureExpired(),
        ):
            replay = apply_scope_change(
                self.owner,
                self.team.pk,
                self.project.pk,
                revision.card_id,
                **data,
                preview_token=token,
                confirmed=True
            )
        self.assertEqual(result.pk, replay.pk)
        self.assertEqual(result.number, 2)
        self.assertEqual(result.previous_version_id, self.version.pk)
        self.assertEqual(revision.last_event.resulting_scope_version_id, result.pk)
        self.assertEqual(revision.last_event.kind, "applied")
        self.assertEqual(ScopeVersion.objects.count(), 2)
        self.version.refresh_from_db()
        self.assertEqual(self.version.body, "Объём 😀: форма обратной связи.")
        self.item.refresh_from_db()
        self.assertEqual(self.item.scope_version_id, self.version.pk)
        with self.assertRaises(ValidationError):
            self.revise(revision)

    def test_event_failure_rolls_back_scope_version(self):
        revision, event = self.accepted()
        data = self.apply_data(revision, event)
        token = self.preview(data, revision.card_id)
        with patch(
            "mainapp.services.change_control._event",
            side_effect=RuntimeError("synthetic fault"),
        ), self.assertRaises(RuntimeError):
            apply_scope_change(
                self.owner,
                self.team.pk,
                self.project.pk,
                revision.card_id,
                **data,
                preview_token=token,
                confirmed=True
            )
        self.assertEqual(ScopeVersion.objects.count(), 1)
        self.assertEqual(ChangeEvent.objects.count(), 2)
        result = apply_scope_change(
            self.owner,
            self.team.pk,
            self.project.pk,
            revision.card_id,
            **data,
            preview_token=token,
            confirmed=True
        )
        self.assertEqual(result.number, 2)

    def test_stale_scope_after_preview_cannot_inherit_approval(self):
        revision, event = self.accepted()
        data = self.apply_data(revision, event)
        token = self.preview(data, revision.card_id)
        newer = confirm_scope(
            self.owner,
            self.team.pk,
            self.project.pk,
            expected_version=self.version.pk,
            body="Другая новая версия.",
            confirmed=True,
        )
        with self.assertRaises(ChangeConflict):
            apply_scope_change(
                self.owner,
                self.team.pk,
                self.project.pk,
                revision.card_id,
                **data,
                preview_token=token,
                confirmed=True
            )
        self.assertEqual(self.project.versions.first().pk, newer.pk)
        self.assertEqual(revision.last_event.kind, "accepted")
        self.assertFalse(ChangeEvent.objects.filter(kind="applied").exists())
