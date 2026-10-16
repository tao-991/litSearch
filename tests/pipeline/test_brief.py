from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from litsearch import db
from litsearch.llm import LLMError
from litsearch.pipeline.brief import build_brief_prompt, make_brief
from tests.llm.fakes import FakeLLM

BRIEF = "# Title\n## Research question\nSomething."


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def make(conn: sqlite3.Connection, llm: FakeLLM, **overrides: Any) -> Any:
    options: dict[str, Any] = {
        "model": "m1",
        "about": "How can coding agents help engineers decide faster?",
        "language": "Chinese",
    }
    options.update(overrides)
    return asyncio.run(make_brief(llm, conn, **options))


def test_the_prompt_carries_the_users_words_and_paper_excerpts() -> None:
    prompt = build_brief_prompt(
        "my interest", [("a.pdf", "Abstract text"), ("b.pdf", "More")]
    )
    assert "my interest" in prompt
    assert (
        "Paper 1 provided by the user (a.pdf)" in prompt and "Abstract text" in prompt
    )
    assert "Paper 2" in prompt
    assert "Paper 1" not in build_brief_prompt("only text")


def test_a_brief_is_written_and_cached(conn: sqlite3.Connection) -> None:
    llm = FakeLLM([BRIEF])
    first = make(conn, llm)
    assert first.text == BRIEF and not first.from_cache
    assert "Chinese" in llm.calls[0]["system"]
    assert "do not invent" in llm.calls[0]["system"].lower()
    again = make(conn, FakeLLM([]))
    assert again.from_cache and again.text == BRIEF


def test_a_fenced_answer_is_unwrapped(conn: sqlite3.Connection) -> None:
    result = make(conn, FakeLLM([f"```markdown\n{BRIEF}\n```"]))
    assert result.text == BRIEF


def test_nothing_to_write_about_is_refused_without_a_call(
    conn: sqlite3.Connection,
) -> None:
    llm = FakeLLM([])
    with pytest.raises(ValueError):
        make(conn, llm, about="   ")
    assert llm.calls == []


def test_an_empty_answer_is_an_error_and_not_cached(conn: sqlite3.Connection) -> None:
    with pytest.raises(LLMError):
        make(conn, FakeLLM(["   "]))
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def test_the_language_and_the_model_are_part_of_the_cache_key(
    conn: sqlite3.Connection,
) -> None:
    make(conn, FakeLLM([BRIEF]))
    assert not make(conn, FakeLLM([BRIEF]), language="English").from_cache
    assert not make(conn, FakeLLM([BRIEF]), model="m2").from_cache
