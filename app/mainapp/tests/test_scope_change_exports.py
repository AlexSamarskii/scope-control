import csv
from io import StringIO
import logging
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from mainapp.services.change_control import ChangeConflict
from mainapp.services.change_exports import csv_text, current_cards, draft_text
from mainapp.models.change_control import ChangeCard
from mainapp.tests.test_scope_change_control import ChangeBase


class ChangeExportTests(ChangeBase):
    def test_draft_is_deterministic_unknown_distinct_from_zero_and_stale_refused(self):
        revision = self.decision(amount=None, currency="", deadline_effect="")
        text = draft_text(
            self.owner, self.team.pk, self.project.pk, revision.card_id, revision.pk
        )
        self.assertIn("Стоимость изменения: Не определена", text)
        self.assertIn("версия 1", text)
        self.assertEqual(
            text,
            draft_text(
                self.owner, self.team.pk, self.project.pk, revision.card_id, revision.pk
            ),
        )
        newer = self.revise(
            revision, amount="0.00", currency="RUB", deadline_effect="Без сдвига срока."
        )
        with self.assertRaises(ChangeConflict):
            draft_text(
                self.owner, self.team.pk, self.project.pk, revision.card_id, revision.pk
            )
        self.assertIn(
            "0.00 RUB",
            draft_text(
                self.owner, self.team.pk, self.project.pk, revision.card_id, newer.pk
            ),
        )

    def test_csv_keeps_current_revision_price_and_answer_after_apply_in_one_query(self):
        revision, event = self.accepted()
        version = self.apply(revision, event)
        with CaptureQueriesContext(connection) as queries:
            rows = list(current_cards(self.owner, self.team.pk, self.project.pk))
        snapshot_sql = [
            q["sql"] for q in queries if 'FROM "mainapp_changecard"' in q["sql"]
        ]
        self.assertEqual(len(snapshot_sql), 1)
        self.assertIn("mainapp_changerevision", snapshot_sql[0])
        self.assertIn("mainapp_changeevent", snapshot_sql[0])
        self.assertEqual(rows[0].snapshot["answer"]["evidence"], event.evidence_text)
        record = list(
            csv.DictReader(
                StringIO(csv_text(self.owner, self.team.pk, self.project.pk))
            )
        )[0]
        self.assertEqual(record["revision_id"], str(revision.pk))
        self.assertEqual(record["amount"], "1500.00")
        self.assertEqual(record["source_version"], "1")
        self.assertEqual(record["resulting_version"], str(version.number))
        self.assertEqual(record["evidence"], "'" + event.evidence_text)
        self.assertNotIn(self.version.body, record.values())

    def test_new_revision_does_not_export_previous_acceptance(self):
        revision, _ = self.accepted()
        newer = self.revise(revision, amount="2000")
        record = list(
            csv.DictReader(
                StringIO(csv_text(self.owner, self.team.pk, self.project.pk))
            )
        )[0]
        self.assertEqual(record["revision_id"], str(newer.pk))
        self.assertEqual(record["amount"], "2000.00")
        self.assertEqual(record["state"], "Черновик изменения")
        self.assertEqual(record["evidence"], "'")

    def test_all_user_text_cells_protected_from_formula_prefixes_with_csv_roundtrip(
        self,
    ):
        for payload in (
            "=SUM(1,2)",
            "+cmd",
            "-1+2",
            "@SUM(1,2)",
            " \t=1",
            "\r\n+1",
            "\ufeff=1",
            '"quoted",\n😀',
        ):
            with self.subTest(payload=repr(payload)):
                item = self.make_request()
                revision = self.decision(
                    item=item,
                    description=payload,
                    rationale=payload,
                    deadline_effect=payload,
                )
                records = list(
                    csv.DictReader(
                        StringIO(csv_text(self.owner, self.team.pk, self.project.pk))
                    )
                )
                record = next(
                    r for r in records if r["card_id"] == str(revision.card_id)
                )
                for field in ("description", "rationale", "deadline_effect"):
                    self.assertEqual(record[field], "'" + payload)

    def test_no_store_safe_attachment_headers_and_non_mutating_export(self):
        revision = self.decision()
        self.client.force_login(self.owner)
        for url in (
            self.url("change_export"),
            self.url("change_draft", card_id=revision.card_id, revision_id=revision.pk),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertIn("attachment; filename=", response["Content-Disposition"])
            self.assertNotIn(revision.description, response["Content-Disposition"])
        self.assertEqual(ChangeCard.objects.count(), 1)

    def test_limit_1000_and_1001_does_not_silently_truncate(self):
        revision = self.decision()
        card = list(current_cards(self.owner, self.team.pk, self.project.pk))[0]

        class Query:
            def __init__(self, size):
                self.size = size

            def __getitem__(self, slice_):
                assert slice_.stop == 1001
                return [card] * self.size

        with patch(
            "mainapp.services.change_exports.current_cards", return_value=Query(1000)
        ):
            self.assertEqual(
                len(
                    list(
                        csv.DictReader(
                            StringIO(
                                csv_text(self.owner, self.team.pk, self.project.pk)
                            )
                        )
                    )
                ),
                1000,
            )
        with patch(
            "mainapp.services.change_exports.current_cards", return_value=Query(1001)
        ), self.assertRaises(ValidationError):
            csv_text(self.owner, self.team.pk, self.project.pk)

    def test_html_escaped_source_not_fetched_and_private_markers_not_logged(self):
        marker = "S3A_PRIVATE_4953<script>alert(1)</script>"
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        installed = []
        seen = set()
        for logger in [root, *logging.Logger.manager.loggerDict.values()]:
            if not isinstance(logger, logging.Logger):
                continue
            for configured in logger.handlers:
                if (
                    isinstance(configured, logging.StreamHandler)
                    and configured is not handler
                    and id(configured) not in seen
                ):
                    seen.add(id(configured))
                    installed.append((configured, configured.stream))
                    configured.setStream(stream)
        try:
            revision = self.decision(description=marker, rationale=marker)
            self.shared(revision)
            event = self.response(
                revision,
                evidence_text=marker,
                source_description="https://example.invalid/private-s3a",
            )
            self.client.force_login(self.owner)
            response = self.client.get(
                self.url("change_detail", card_id=revision.card_id)
            )
            self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
            self.assertNotContains(response, "<script>alert(1)</script>")
            self.assertNotContains(
                response, 'href="https://example.invalid/private-s3a"'
            )
            self.client.get(self.url("change_export"))
            with self.assertRaises(ValidationError):
                self.response(revision, evidence_text=marker)
            self.client.post(
                self.url("change_revise", card_id=revision.card_id),
                {"description": marker},
            )
            data = self.apply_data(revision, event)
            token = self.preview(data, revision.card_id)
            with patch(
                "mainapp.services.change_control._event",
                side_effect=RuntimeError(marker),
            ):
                failed = self.client.post(
                    self.url("change_apply", card_id=revision.card_id),
                    {
                        **data,
                        "preview_token": token,
                        "confirmed": "on",
                        "action": "confirm",
                    },
                )
            self.assertEqual(failed.status_code, 500)
            self.assertNotIn("S3A_PRIVATE_4953", failed.content.decode())
            self.assertEqual(revision.last_event.kind, "accepted")
        finally:
            for configured, original in installed:
                configured.setStream(original)
            root.removeHandler(handler)
        self.assertNotIn("S3A_PRIVATE_4953", stream.getvalue())
        self.assertNotIn("private-s3a", stream.getvalue())
