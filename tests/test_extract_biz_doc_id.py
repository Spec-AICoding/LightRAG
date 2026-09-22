"""Unit tests for :func:`lightrag.lightrag._extract_biz_doc_id`.

The helper extracts the onyx document URL from the top-level string ``"id"``
of a custom-chunk JSON payload (fork-custom for chunk ownership persistence).
Every non-matching input — plain text, invalid JSON, non-object JSON, a
missing or non-string ``"id"`` — must yield ``""`` without raising, so
non-onyx content is stored as empty rather than dropped.
"""

import json

import pytest

from lightrag.lightrag import _extract_biz_doc_id

pytestmark = pytest.mark.offline


def test_json_object_with_string_id_returns_verbatim():
    payload = '{"id": "https://wpf0310.atlassian.net/browse/SCRUM-1", "text": "hi"}'
    assert (
        _extract_biz_doc_id(payload) == "https://wpf0310.atlassian.net/browse/SCRUM-1"
    )


def test_url_with_path_and_query_preserved():
    url = "https://host.example.com/wiki/page?space=ENG&id=42#section-3"
    assert _extract_biz_doc_id(json.dumps({"id": url})) == url


def test_non_ascii_id_preserved():
    assert _extract_biz_doc_id('{"id": "文档-001"}') == "文档-001"


def test_empty_string_id_returns_empty():
    assert _extract_biz_doc_id('{"id": ""}') == ""


def test_plain_text_returns_empty():
    assert _extract_biz_doc_id("just a plain sentence") == ""


def test_invalid_json_returns_empty():
    assert _extract_biz_doc_id('{"id": "broken"') == ""


def test_json_array_returns_empty():
    assert _extract_biz_doc_id('["id", "value"]') == ""


def test_json_scalar_returns_empty():
    assert _extract_biz_doc_id('"https://host/doc"') == ""
    assert _extract_biz_doc_id("42") == ""


def test_dict_without_id_returns_empty():
    assert _extract_biz_doc_id('{"text": "no id here"}') == ""


def test_non_string_id_returns_empty():
    assert _extract_biz_doc_id('{"id": 42}') == ""
    assert _extract_biz_doc_id('{"id": null}') == ""
    assert _extract_biz_doc_id('{"id": {"nested": "x"}}') == ""


def test_non_str_input_never_raises():
    # Defensive: callers pass strings, but the helper must not raise on
    # anything that sneaks through (TypeError guard).
    assert _extract_biz_doc_id(None) == ""
