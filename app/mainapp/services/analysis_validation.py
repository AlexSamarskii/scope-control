"""Bounded JSON and source validation; quote matching is not semantic proof."""

import json


class InvalidAnalysis(ValueError):
    def __init__(self):
        super().__init__("invalid_response")


def _invalid(*args):
    raise InvalidAnalysis()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _text(value, limit):
    if type(value) is not str or not value.strip() or len(value) > limit:
        _invalid()


def validate_response(raw, sources):
    if type(raw) is not str:
        _invalid()
    try:
        if len(raw.encode("utf-8")) > 32768:
            _invalid()
        result = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise InvalidAnalysis() from None
    if type(result) is not dict or set(result) != {
        "schema_version",
        "category",
        "rationale",
        "citations",
        "questions",
    }:
        _invalid()
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        _invalid()
    category = result["category"]
    if type(category) is not str or category not in {
        "included",
        "possible_change",
        "insufficient_evidence",
    }:
        _invalid()
    _text(result["rationale"], 2000)
    citations, questions = result["citations"], result["questions"]
    if (
        type(citations) is not list
        or len(citations) > 8
        or type(questions) is not list
        or len(questions) > 5
    ):
        _invalid()
    for question in questions:
        _text(question, 500)
    scope_citation = False
    for citation in citations:
        if type(citation) is not dict or set(citation) != {
            "source_id",
            "start",
            "end",
            "quote",
        }:
            _invalid()
        source_id, start, end = (
            citation["source_id"],
            citation["start"],
            citation["end"],
        )
        if type(source_id) is not str or source_id not in sources:
            _invalid()
        if type(start) is not int or type(end) is not int:
            _invalid()
        _text(citation["quote"], 2000)
        source = sources[source_id]
        if (
            not 0 <= start < end <= len(source)
            or source[start:end] != citation["quote"]
        ):
            _invalid()
        scope_citation |= source_id.startswith("scope:")
    if category == "insufficient_evidence":
        if not questions:
            _invalid()
    elif not scope_citation:
        _invalid()
    return result


def parse_object(raw, *, byte_limit=32768):
    """Strict bounded object parsing shared by the provider and domain validator."""
    if type(raw) is bytes:
        if len(raw) > byte_limit:
            raise InvalidAnalysis()
        try:
            raw = raw.decode("utf-8")
        except UnicodeError:
            raise InvalidAnalysis() from None
    if type(raw) is not str:
        raise InvalidAnalysis()
    try:
        if len(raw.encode("utf-8")) > byte_limit:
            raise InvalidAnalysis()
        result = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise InvalidAnalysis() from None
    if type(result) is not dict:
        raise InvalidAnalysis()
    return result
