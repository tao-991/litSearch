from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from litsearch import db
from litsearch.llm import LLMError
from litsearch.pipeline.queries import (
    NotCachedError,
    QueryPlan,
    generate_queries,
    parse_queries,
    queries_file_text,
    write_queries_file,
)
from tests.llm.fakes import FakeLLM

PROJECT = (
    "# Project\n## Research question\n"
    "How can a coding agent help engineers decide faster?"
)


def reply(*queries: Any) -> str:
    return json.dumps(
        {
            "queries": [
                {"query": q, "angle": f"angle of {q}"} if isinstance(q, str) else q
                for q in queries
            ]
        }
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def go(coro: Any) -> Any:
    return asyncio.run(coro)


def gen(conn: sqlite3.Connection, llm: FakeLLM | None, **overrides: Any) -> QueryPlan:
    options: dict[str, Any] = {
        "model": "m1",
        "project": PROJECT,
        "language": "Chinese",
        "max_queries": 15,
    }
    options.update(overrides)
    return go(generate_queries(llm, conn, **options))


# ------------------------------------------------------------------------ parsing


def test_parse_accepts_objects_strings_and_fenced_json() -> None:
    body = (
        '{"queries": [{"query": "code review agents", "angle": "review"}, '
        '"pull request acceptance"]}'
    )
    text = f"```json\n{body}\n```"
    queries, rejected, over = parse_queries(text, 10)
    assert [(q.text, q.angle) for q in queries] == [
        ("code review agents", "review"),
        ("pull request acceptance", ""),
    ]
    assert (rejected, over) == (0, 0)


def test_parse_accepts_a_bare_list() -> None:
    queries, _, _ = parse_queries('["one two", "three four"]', 10)
    assert [q.text for q in queries] == ["one two", "three four"]


def test_parse_cleans_rejects_and_deduplicates() -> None:
    text = reply(
        '  "human   in the loop"  ',  # quotes and spacing removed
        "Human in the Loop",  # duplicate, ignoring case
        "",  # empty
        "word " * 11,  # too many words
        "x" * 130,  # too long
        {"query": 42, "angle": "a"},  # not text
        "trust, in automation.",
    )
    queries, rejected, over = parse_queries(text, 10)
    assert [q.text for q in queries] == ["human in the loop", "trust, in automation"]
    assert rejected == 5
    assert over == 0


def test_the_ceiling_keeps_the_first_queries_and_counts_the_rest() -> None:
    queries, rejected, over = parse_queries(
        reply(*[f"query number {i}" for i in range(10)]), 4
    )
    assert [q.text for q in queries] == [f"query number {i}" for i in range(4)]
    assert (rejected, over) == (0, 6)


@pytest.mark.parametrize(
    "text", ["", "no json", '{"other": 1}', '{"queries": "x"}', "42"]
)
def test_parse_rejects_replies_without_a_query_list(text: str) -> None:
    with pytest.raises(ValueError):
        parse_queries(text, 10)


# ---------------------------------------------------------------------- generating


def test_the_model_decides_how_many_queries(conn: sqlite3.Connection) -> None:
    three = gen(conn, FakeLLM([reply("a b", "c d", "e f")]), project="short project")
    nine = gen(
        conn,
        FakeLLM([reply(*[f"topic {i} words" for i in range(9)])]),
        project="long project",
    )
    assert (len(three.queries), len(nine.queries)) == (3, 9)


def test_the_request_describes_the_task_and_carries_the_project(
    conn: sqlite3.Connection,
) -> None:
    llm = FakeLLM([reply("code review agents")])
    gen(conn, llm, max_queries=12, language="Chinese")
    call = llm.calls[0]
    assert call["model"] == "m1"
    assert call["json_mode"] is True
    assert "12" not in call["system"]  # the ceiling is not shown to the model
    assert "Never write more than" not in call["system"]
    assert "Do not pad" in call["system"]
    assert "Do not invent names" in call["system"]
    assert "angle" in call["system"] and "Chinese" in call["system"]
    assert "write the queries in English" in call["system"].replace("Always ", "")
    assert "exactly" not in call["system"].lower()  # no fixed number is demanded
    assert PROJECT.strip() in call["user"]


def test_an_answer_is_cached_and_not_asked_twice(conn: sqlite3.Connection) -> None:
    llm = FakeLLM([reply("code review agents", "pull request acceptance")])
    first = gen(conn, llm)
    again = gen(conn, FakeLLM([]))  # a second model call would fail the test
    assert not first.from_cache and again.from_cache
    assert [q.text for q in again.queries] == [q.text for q in first.queries]
    assert (first.input_tokens, first.output_tokens) == (100, 50)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "m2"},
        {"language": "English"},
        {"project": PROJECT + " Also trust."},
    ],
)
def test_a_change_of_any_input_asks_the_model_again(
    conn: sqlite3.Connection, change: dict[str, Any]
) -> None:
    gen(conn, FakeLLM([reply("code review agents")]))
    llm = FakeLLM([reply("something else entirely")])
    plan = gen(conn, llm, **change)
    assert not plan.from_cache
    assert len(llm.calls) == 1


def test_one_cached_answer_serves_every_ceiling(conn: sqlite3.Connection) -> None:
    gen(
        conn, FakeLLM([reply(*[f"query number {i}" for i in range(9)])]), max_queries=50
    )
    smaller = gen(conn, FakeLLM([]), max_queries=4)  # no second model call
    assert smaller.from_cache
    assert [q.text for q in smaller.queries] == [f"query number {i}" for i in range(4)]
    assert smaller.n_over_ceiling == 5


def test_cache_only_never_calls_the_model(conn: sqlite3.Connection) -> None:
    with pytest.raises(NotCachedError):
        gen(conn, FakeLLM([]), cache_only=True)
    gen(conn, FakeLLM([reply("code review agents")]))
    assert gen(conn, FakeLLM([]), cache_only=True).from_cache
    with pytest.raises(NotCachedError):
        gen(conn, None, project="a different project")  # no model client, no cache


def test_an_unreadable_reply_is_retried_once_with_a_reminder(
    conn: sqlite3.Connection,
) -> None:
    llm = FakeLLM(["Sorry, here are some ideas: foo", reply("code review agents")])
    plan = gen(conn, llm)
    assert [q.text for q in plan.queries] == ["code review agents"]
    assert len(llm.calls) == 2
    assert "ONLY the JSON" in llm.calls[1]["user"]
    assert "ONLY the JSON" not in llm.calls[0]["user"]


@pytest.mark.parametrize("bad", ["no json at all", reply(), reply("", "x" * 200)])
def test_two_unusable_replies_are_an_error_and_nothing_is_cached(
    conn: sqlite3.Connection, bad: str
) -> None:
    llm = FakeLLM([bad, bad])
    with pytest.raises(LLMError) as info:
        gen(conn, llm)
    assert "usable queries" in info.value.message
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def test_a_model_failure_propagates(conn: sqlite3.Connection) -> None:
    with pytest.raises(LLMError) as info:
        gen(conn, FakeLLM([LLMError("auth", "refused")]))
    assert info.value.kind == "auth"


def test_a_damaged_cache_entry_counts_as_a_miss(conn: sqlite3.Connection) -> None:
    gen(conn, FakeLLM([reply("code review agents")]))
    with conn:
        conn.execute("UPDATE llm_cache SET output = 'garbage'")
    llm = FakeLLM([reply("pull request acceptance")])
    plan = gen(conn, llm)
    assert [q.text for q in plan.queries] == ["pull request acceptance"]


def test_the_ceiling_is_reported_in_the_plan(conn: sqlite3.Connection) -> None:
    plan = gen(
        conn, FakeLLM([reply(*[f"query number {i}" for i in range(9)])]), max_queries=5
    )
    assert len(plan.queries) == 5
    assert plan.n_over_ceiling == 4


# --------------------------------------------------------------------------- file


def test_the_queries_file_has_one_query_per_line_and_angles_as_comments() -> None:
    plan = QueryPlan(
        queries=[
            *parse_queries(reply("code review agents", "trust automation"), 5)[0],
        ],
        n_rejected=0,
        n_over_ceiling=0,
        from_cache=False,
        model="m",
    )
    text = queries_file_text(plan, "demo")
    lines = text.splitlines()
    assert "code review agents" in lines and "trust automation" in lines
    assert "# angle of code review agents" in lines
    assert lines[0].startswith("# Search queries for demo")


def test_writing_the_queries_file_never_overwrites_edits_unless_forced(
    tmp_path: Path,
) -> None:
    plan = QueryPlan(parse_queries(reply("code review agents"), 5)[0], 0, 0, False, "m")
    path = tmp_path / "sub" / "queries.txt"
    write_queries_file(plan, path)
    path.write_text("my own edits", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_queries_file(plan, path)
    assert path.read_text(encoding="utf-8") == "my own edits"
    write_queries_file(plan, path, force=True)
    assert "code review agents" in path.read_text(encoding="utf-8")
