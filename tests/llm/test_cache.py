from __future__ import annotations

import sqlite3

import pytest

from litsearch import db
from litsearch.llm.cache import get_cached, input_hash, put_cached


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def test_input_hash_depends_on_every_part_and_their_boundaries() -> None:
    assert input_hash("a", "b") == input_hash("a", "b")
    assert input_hash("a", "b") != input_hash("b", "a")
    assert input_hash("a", "b") != input_hash("ab", "")
    assert input_hash("a", "b") != input_hash("a", "b", "")
    assert len(input_hash("x")) == 64


def test_a_miss_returns_none_and_a_hit_returns_the_stored_text(
    conn: sqlite3.Connection,
) -> None:
    assert get_cached(conn, "queries", "h1", "v1", "model-a") is None
    put_cached(
        conn,
        "queries",
        "h1",
        "v1",
        "model-a",
        '{"queries": []}',
        input_tokens=10,
        output_tokens=5,
    )
    assert get_cached(conn, "queries", "h1", "v1", "model-a") == '{"queries": []}'
    row = conn.execute("SELECT input_tokens, output_tokens FROM llm_cache").fetchone()
    assert (row[0], row[1]) == (10, 5)


@pytest.mark.parametrize(
    "other",
    [
        ("brief", "h1", "v1", "model-a"),
        ("queries", "h2", "v1", "model-a"),
        ("queries", "h1", "v2", "model-a"),
        ("queries", "h1", "v1", "model-b"),
    ],
)
def test_the_key_is_task_input_prompt_version_and_model(
    conn: sqlite3.Connection, other: tuple[str, str, str, str]
) -> None:
    put_cached(conn, "queries", "h1", "v1", "model-a", "answer")
    assert get_cached(conn, *other) is None


def test_putting_again_replaces_the_answer(conn: sqlite3.Connection) -> None:
    put_cached(conn, "queries", "h1", "v1", "m", "old")
    put_cached(conn, "queries", "h1", "v1", "m", "new")
    assert get_cached(conn, "queries", "h1", "v1", "m") == "new"
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 1
