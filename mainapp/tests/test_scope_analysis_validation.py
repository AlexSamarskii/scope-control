from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4
from django.test import SimpleTestCase
from mainapp.services.analysis_validation import InvalidAnalysis, validate_response


class ResponseValidationTests(SimpleTestCase):
    def setUp(self):
        self.scope_id = f"scope:{uuid4()}:body"
        self.request_id = f"request:{uuid4()}:body"
        self.sources = {
            self.scope_id: "😀 форма  связи\nещё",
            self.request_id: "запрос",
        }
        self.valid = {
            "schema_version": 1,
            "category": "included",
            "rationale": "Форма указана.",
            "citations": [
                {"source_id": self.scope_id, "start": 2, "end": 7, "quote": "форма"}
            ],
            "questions": [],
        }

    def validate(self, result):
        return validate_response(json.dumps(result, ensure_ascii=False), self.sources)

    def test_three_categories_and_exact_unicode_offsets(self):
        self.assertEqual(self.validate(self.valid), self.valid)
        for category in ["possible_change", "insufficient_evidence"]:
            result = {
                **self.valid,
                "category": category,
                "questions": ["Какая правка нужна?"],
            }
            if category == "insufficient_evidence":
                result["citations"] = []
            self.assertEqual(self.validate(result), result)
        result = deepcopy(self.valid)
        result["citations"][0].update(start=0, end=1, quote="😀")
        self.assertEqual(self.validate(result), result)

    def test_source_type_offset_and_verbatim_failures(self):
        for change in [
            dict(source_id=f"scope:{uuid4()}:body"),
            dict(source_id=self.scope_id.replace(":body", ":exclusions")),
            dict(quote="ФОРМА"),
            dict(quote="форма "),
            dict(start=True),
            dict(end=1.2),
            dict(start=-1),
            dict(end=999),
            dict(start=7, end=2),
            dict(start=2, end=2),
            dict(quote=""),
            dict(quote="x" * 2001),
            dict(source_id=[]),
            dict(command="approve"),
        ]:
            result = deepcopy(self.valid)
            result["citations"][0].update(change)
            with self.subTest(change=change), self.assertRaises(InvalidAnalysis):
                self.validate(result)

    def test_missing_unknown_and_mistyped_fields(self):
        changes = [
            dict(schema_version=True),
            dict(schema_version=2),
            dict(category=[]),
            dict(category="approved"),
            dict(rationale="  "),
            dict(rationale="x" * 2001),
            dict(rationale={}),
            dict(citations={}),
            dict(citations=[None]),
            dict(citations=[self.valid["citations"][0]] * 9),
            dict(questions={}),
            dict(questions=["q"] * 6),
            dict(questions=["x" * 501]),
            dict(questions=[" "]),
            dict(questions=[False]),
            dict(price=999),
            dict(approval=True),
            dict(deadline="tomorrow"),
            dict(command="confirm_scope"),
        ]
        for change in changes:
            with self.subTest(change=list(change)), self.assertRaises(InvalidAnalysis):
                self.validate({**self.valid, **change})
        for key in self.valid:
            result = dict(self.valid)
            del result[key]
            with self.subTest(missing=key), self.assertRaises(InvalidAnalysis):
                self.validate(result)

    def test_category_evidence_requirements(self):
        for result in [
            {**self.valid, "citations": []},
            {**self.valid, "category": "possible_change", "citations": []},
            {**self.valid, "category": "insufficient_evidence"},
        ]:
            with self.assertRaises(InvalidAnalysis):
                self.validate(result)
        result = deepcopy(self.valid)
        result["citations"][0].update(
            source_id=self.request_id, start=0, end=6, quote="запрос"
        )
        with self.assertRaises(InvalidAnalysis):
            self.validate(result)

    def test_bounded_strict_json_parser(self):
        for raw in [
            None,
            {},
            b"{}",
            "{",
            "[]",
            "null",
            '{"schema_version":1,"schema_version":1}',
            '{"x":NaN}',
            '{"x":Infinity}',
            "[" * 2000 + "]" * 2000,
            '"' + "😀" * 10000 + '"',
            '"\ud800"',
        ]:
            with self.subTest(kind=type(raw).__name__), self.assertRaises(
                InvalidAnalysis
            ):
                validate_response(raw, self.sources)
        raw = json.dumps(self.valid)
        self.assertEqual(
            validate_response(raw + " " * (32768 - len(raw.encode())), self.sources),
            self.valid,
        )
        with self.assertRaises(InvalidAnalysis):
            validate_response(raw + " " * (32769 - len(raw.encode())), self.sources)
        nested = deepcopy(self.valid)
        nested["rationale"] = {"a": {"b": {"c": []}}}
        with self.assertRaises(InvalidAnalysis):
            self.validate(nested)

    def test_synthetic_corpus_uses_real_source_spans(self):
        cases = json.loads(
            (Path(__file__).parent / "fixtures/scope_analysis_cases.json").read_text()
        )
        categories = set()
        for case in cases:
            sources = {
                self.scope_id: case["scope"],
                self.scope_id.replace(":body", ":exclusions"): case.get(
                    "exclusions", ""
                ),
            }
            citations = []
            if case.get("quote"):
                source_id = self.scope_id.rsplit(":", 1)[0] + ":" + case["field"]
                start = sources[source_id].index(case["quote"])
                citations = [
                    dict(
                        source_id=source_id,
                        start=start,
                        end=start + len(case["quote"]),
                        quote=case["quote"],
                    )
                ]
            result = dict(
                schema_version=1,
                category=case["category"],
                rationale=case["rationale"],
                citations=citations,
                questions=case["questions"],
            )
            categories.add(validate_response(json.dumps(result), sources)["category"])
        self.assertEqual(
            categories, {"included", "possible_change", "insufficient_evidence"}
        )
