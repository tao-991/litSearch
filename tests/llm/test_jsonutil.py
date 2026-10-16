from __future__ import annotations

import pytest

from litsearch.llm.jsonutil import extract_json


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("  [1, 2, 3]\n", [1, 2, 3]),
        ('```json\n{"queries": ["x"]}\n```', {"queries": ["x"]}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Sure! Here you go:\n{"a": {"b": 2}}\nHope that helps.', {"a": {"b": 2}}),
        ('Result: [{"id": 1}, {"id": 2}] done', [{"id": 1}, {"id": 2}]),
        ('Some text ```json\n{"a": 1}\n``` more text', {"a": 1}),
        ('{"text": "has } braces { inside"}', {"text": "has } braces { inside"}),
    ],
)
def test_json_is_found_in_typical_model_replies(text: str, expected: object) -> None:
    assert extract_json(text) == expected


@pytest.mark.parametrize(
    "text", ["", "no json here", "{broken", '{"a": }', "```json\nnope\n```"]
)
def test_a_reply_without_valid_json_is_an_error(text: str) -> None:
    with pytest.raises(ValueError):
        extract_json(text)
