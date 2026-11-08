from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from mainapp.models import Team, TeamMembership
from mainapp.models.change_control import ChangeCard, ChangeRevision, ChangeEvent
from mainapp.models.scope_control import ScopeVersion
from mainapp.services.analysis import create_request
from mainapp.services.scope_control import create_project, confirm_scope
from mainapp.services.change_control import (
    record_decision,
    revise_decision,
    record_shared,
    record_client_response,
    preview_scope_change,
    apply_scope_change,
)
from usermodel.models import User


class ChangeConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.owner = User.objects.create_user(email="change-race-a@example.com")
        self.manager = User.objects.create_user(email="change-race-b@example.com")
        self.team = Team.objects.create(name="Синтетика", slug="change-race")
        TeamMembership.objects.create(team=self.team, user=self.owner, role="owner")
        TeamMembership.objects.create(team=self.team, user=self.manager, role="admin")
        self.project = create_project(self.owner, self.team.pk, name="Synthetic race")
        self.version = confirm_scope(
            self.owner, self.team.pk, self.project.pk, body="v1", confirmed=True
        )
        self.item = create_request(
            self.owner,
            self.team.pk,
            self.project.pk,
            version_id=self.version.pk,
            body="Новая страница",
            submission_key=uuid4(),
        )
        self.fields = dict(
            category="possible_change",
            description="Добавить страницу",
            rationale="Она исключена",
            amount="100",
            currency="RUB",
            deadline_effect="Один день",
            confirmed=True,
        )

    def actor(self, manager=False):
        return User.objects.get(pk=self.manager.pk if manager else self.owner.pk)

    def decision(self, manager=False, key=None):
        return record_decision(
            self.actor(manager),
            self.team.pk,
            self.project.pk,
            self.item.pk,
            submission_key=key or uuid4(),
            **self.fields
        )

    def race(self, left, right):
        barrier = Barrier(2)

        def call(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    return fn()
                except ValidationError:
                    return "blocked"
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, fn) for fn in [left, right]]
            return [f.result(timeout=30) for f in futures]

    def shared(self):
        revision = self.decision()
        event = record_shared(
            self.owner,
            self.team.pk,
            self.project.pk,
            revision.card_id,
            expected_revision_id=revision.pk,
            submission_key=uuid4(),
            confirmed=True,
        )
        return revision, event

    def accepted(self):
        revision, shared = self.shared()
        event = record_client_response(
            self.owner,
            self.team.pk,
            self.project.pk,
            revision.card_id,
            kind="accepted",
            expected_revision_id=revision.pk,
            expected_event_id=shared.pk,
            sender_name="Синтетика",
            occurred_at=timezone.now() - timedelta(minutes=1),
            evidence_text="100 RUB и день",
            submission_key=uuid4(),
            confirmed=True,
        )
        return revision, event

    def revision_call(self, revision, event):
        return revise_decision(
            self.actor(True),
            self.team.pk,
            self.project.pk,
            revision.card_id,
            expected_revision_id=revision.pk,
            expected_event_id=event.pk,
            submission_key=uuid4(),
            **self.fields
        ).pk

    def apply_data(self, revision, event):
        data = dict(
            expected_revision_id=revision.pk,
            expected_event_id=event.pk,
            expected_version=self.version.pk,
            body="v2 with approved change",
            submission_key=uuid4(),
        )
        token = preview_scope_change(
            self.owner, self.team.pk, self.project.pk, revision.card_id, **data
        )
        return {**data, "preview_token": token, "confirmed": True}

    def test_two_managers_create_one_card_for_request(self):
        result = self.race(lambda: self.decision().pk, lambda: self.decision(True).pk)
        self.assertEqual(result.count("blocked"), 1)
        self.assertEqual(ChangeCard.objects.count(), 1)
        self.assertEqual(ChangeRevision.objects.count(), 1)

    def test_duplicate_create_key_returns_same_revision(self):
        key = uuid4()
        result = self.race(
            lambda: self.decision(key=key).pk, lambda: self.decision(key=key).pk
        )
        self.assertNotIn("blocked", result)
        self.assertEqual(result[0], result[1])
        self.assertEqual(ChangeCard.objects.count(), 1)

    def test_revision_racing_acceptance_never_inherits_approval(self):
        revision, shared = self.shared()

        def accept():
            return record_client_response(
                self.actor(),
                self.team.pk,
                self.project.pk,
                revision.card_id,
                kind="accepted",
                expected_revision_id=revision.pk,
                expected_event_id=shared.pk,
                sender_name="Клиент",
                occurred_at=timezone.now() - timedelta(minutes=1),
                evidence_text="Принимаю",
                submission_key=uuid4(),
                confirmed=True,
            ).pk

        result = self.race(lambda: self.revision_call(revision, shared), accept)
        self.assertEqual(result.count("blocked"), 1)
        head = revision.card.current_revision
        if result[0] != "blocked":
            self.assertEqual(head.number, 2)
            self.assertEqual(head.state, "draft")
        else:
            self.assertEqual(head.number, 1)
            self.assertEqual(head.state, "accepted")

    def test_revision_racing_apply_commits_one_consistent_outcome(self):
        revision, event = self.accepted()
        data = self.apply_data(revision, event)
        result = self.race(
            lambda: self.revision_call(revision, event),
            lambda: apply_scope_change(
                self.actor(), self.team.pk, self.project.pk, revision.card_id, **data
            ).pk,
        )
        self.assertEqual(result.count("blocked"), 1)
        applied = result[1] != "blocked"
        self.assertEqual(ScopeVersion.objects.count(), 2 if applied else 1)
        self.assertEqual(
            ChangeEvent.objects.filter(kind="applied").count(), int(applied)
        )
        self.assertEqual(
            revision.card.current_revision.state, "applied" if applied else "draft"
        )

    def test_two_apply_keys_create_only_one_new_scope(self):
        revision, event = self.accepted()
        one = self.apply_data(revision, event)
        two = self.apply_data(revision, event)
        result = self.race(
            lambda: apply_scope_change(
                self.actor(), self.team.pk, self.project.pk, revision.card_id, **one
            ).pk,
            lambda: apply_scope_change(
                self.actor(), self.team.pk, self.project.pk, revision.card_id, **two
            ).pk,
        )
        self.assertEqual(result.count("blocked"), 1)
        self.assertEqual(ScopeVersion.objects.count(), 2)
        self.assertEqual(ChangeEvent.objects.filter(kind="applied").count(), 1)

    def test_same_apply_key_replays_same_scope(self):
        revision, event = self.accepted()
        data = self.apply_data(revision, event)
        result = self.race(
            lambda: apply_scope_change(
                self.actor(), self.team.pk, self.project.pk, revision.card_id, **data
            ).pk,
            lambda: apply_scope_change(
                self.actor(), self.team.pk, self.project.pk, revision.card_id, **data
            ).pk,
        )
        self.assertNotIn("blocked", result)
        self.assertEqual(result[0], result[1])
        self.assertEqual(ScopeVersion.objects.count(), 2)
        self.assertEqual(ChangeEvent.objects.filter(kind="applied").count(), 1)

    def test_apply_racing_existing_scope_confirmation_cannot_lose_new_version(self):
        revision, event = self.accepted()
        data = self.apply_data(revision, event)
        result = self.race(
            lambda: apply_scope_change(
                self.actor(), self.team.pk, self.project.pk, revision.card_id, **data
            ).pk,
            lambda: confirm_scope(
                self.actor(True),
                self.team.pk,
                self.project.pk,
                expected_version=self.version.pk,
                body="v2 manual S1",
                confirmed=True,
            ).pk,
        )
        self.assertEqual(result.count("blocked"), 1)
        self.assertEqual(
            list(self.project.versions.values_list("number", flat=True)), [2, 1]
        )
        self.assertEqual(
            ChangeEvent.objects.filter(kind="applied").exists(), result[0] != "blocked"
        )
        self.version.refresh_from_db()
        self.assertEqual(self.version.body, "v1")
