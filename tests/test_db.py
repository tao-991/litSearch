from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from litsearch import db

EXPECTED_TABLES = {
    "runs",
    "queries",
    "papers",
    "paper_records",
    "hits",
    "scores",
    "edges",
    "source_runs",
    "fetches",
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(tmp_path / "nested" / "lit.db")
    db.init_schema(c)
    yield c
    c.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def _paper(conn: sqlite3.Connection, title: str = "T", **cols: object) -> int:
    names = ["title", *cols]
    sql = (
        f"INSERT INTO papers ({', '.join(names)}) "
        f"VALUES ({', '.join('?' * len(names))})"
    )
    with conn:
        cur = conn.execute(sql, [title, *cols.values()])
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_connect_creates_parent_directory_and_enables_foreign_keys(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    assert (tmp_path / "nested" / "lit.db").exists()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_schema_creates_all_tables(conn: sqlite3.Connection) -> None:
    assert _tables(conn) == EXPECTED_TABLES
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_init_schema_is_idempotent_and_keeps_data(conn: sqlite3.Connection) -> None:
    run_id = db.create_run(conn, "hash")
    db.init_schema(conn)
    db.init_schema(conn)
    assert db.get_run(conn, run_id) is not None
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_newer_database_is_refused(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(db.SchemaError):
        db.init_schema(conn)


def test_in_memory_database_works() -> None:
    c = db.connect(":memory:")
    db.init_schema(c)
    assert _tables(c) == EXPECTED_TABLES


def test_json_round_trip_keeps_unicode() -> None:
    value = {"authors": ["Zoë Müller", "李雷"], "n": 3, "none": None}
    text = db.dumps_json(value)
    assert "李雷" in text  # not escaped
    assert db.loads_json(text) == value
    assert db.loads_json(None) is None


def test_create_get_and_finish_run(conn: sqlite3.Connection) -> None:
    run_id = db.create_run(conn, "abc", {"rounds": 2, "sources": ["openalex"]})
    run = db.get_run(conn, run_id)
    assert run is not None
    assert run["project_hash"] == "abc"
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert db.loads_json(run["params"]) == {"rounds": 2, "sources": ["openalex"]}

    db.set_run_status(conn, run_id, "completed")
    run = db.get_run(conn, run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["finished_at"] is not None


def test_run_status_is_constrained(conn: sqlite3.Connection) -> None:
    run_id = db.create_run(conn, "abc")
    with pytest.raises(sqlite3.IntegrityError):
        db.set_run_status(conn, run_id, "bogus")


def test_get_run_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert db.get_run(conn, 999) is None


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    paper_id = _paper(conn)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "INSERT INTO hits (run_id, source, paper_id) VALUES (?, ?, ?)",
            (999, "openalex", paper_id),
        )


def test_duplicate_normalized_ids_are_rejected(conn: sqlite3.Connection) -> None:
    _paper(conn, doi="10.1/abc", arxiv_id="2501.00001", openalex_id="W1", s2_id="s")
    for col, value in [
        ("doi", "10.1/abc"),
        ("arxiv_id", "2501.00001"),
        ("openalex_id", "W1"),
        ("s2_id", "s"),
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            _paper(conn, title="other", **{col: value})


def test_papers_without_ids_do_not_collide(conn: sqlite3.Connection) -> None:
    ids = {_paper(conn, title=f"paper {i}") for i in range(3)}
    assert len(ids) == 3


def test_paper_records_unique_per_source_and_cascade(conn: sqlite3.Connection) -> None:
    paper_id = _paper(conn)
    sql = (
        "INSERT INTO paper_records (paper_id, source, source_id, raw) "
        "VALUES (?, ?, ?, ?)"
    )
    with conn:
        conn.execute(sql, (paper_id, "openalex", "W1", "{}"))
        conn.execute(sql, (paper_id, "arxiv", "2501.00001", "{}"))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (paper_id, "openalex", "W1", "{}"))

    with conn:
        conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
    assert conn.execute("SELECT COUNT(*) FROM paper_records").fetchone()[0] == 0


def test_hits_allow_snowball_rows_without_a_query(conn: sqlite3.Connection) -> None:
    run_id = db.create_run(conn, "h")
    paper_id = _paper(conn)
    with conn:
        conn.execute(
            "INSERT INTO hits (run_id, query_id, source, paper_id, round) "
            "VALUES (?, NULL, 'openalex', ?, 1)",
            (run_id, paper_id),
        )
    assert conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0] == 1


def test_query_text_is_unique_per_run_and_origin_constrained(
    conn: sqlite3.Connection,
) -> None:
    run_id = db.create_run(conn, "h")
    other_run = db.create_run(conn, "h")
    sql = "INSERT INTO queries (run_id, text, origin) VALUES (?, ?, ?)"
    with conn:
        conn.execute(sql, (run_id, "agent review", "llm"))
        conn.execute(sql, (other_run, "agent review", "user"))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (run_id, "agent review", "user"))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (run_id, "something else", "robot"))


def test_score_cache_key_is_the_primary_key(conn: sqlite3.Connection) -> None:
    paper_id = _paper(conn)
    sql = (
        "INSERT INTO scores (paper_id, project_hash, input_hash, prompt_version, "
        "model, score) VALUES (?, ?, ?, ?, ?, ?)"
    )
    with conn:
        conn.execute(sql, (paper_id, "p", "in1", "v1", "m", 7.0))
        # any single key component changing is a different cache entry
        conn.execute(sql, (paper_id, "p2", "in1", "v1", "m", 6.0))
        conn.execute(sql, (paper_id, "p", "in2", "v1", "m", 5.0))
        conn.execute(sql, (paper_id, "p", "in1", "v2", "m", 4.0))
        conn.execute(sql, (paper_id, "p", "in1", "v1", "m2", 3.0))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (paper_id, "p", "in1", "v1", "m", 9.0))


def test_edges_are_unique_and_need_existing_papers(conn: sqlite3.Connection) -> None:
    a, b = _paper(conn, "A"), _paper(conn, "B")
    sql = "INSERT INTO edges (src_paper_id, dst_paper_id) VALUES (?, ?)"
    with conn:
        conn.execute(sql, (a, b))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (a, b))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (a, 999))


def test_source_runs_status_and_uniqueness(conn: sqlite3.Connection) -> None:
    run_id = db.create_run(conn, "h")
    sql = "INSERT INTO source_runs (run_id, source, status) VALUES (?, ?, ?)"
    for status in ("ok", "blocked", "rate_limited", "error", "skipped"):
        with conn:
            conn.execute(sql, (run_id, f"src-{status}", status))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (run_id, "src-ok", "ok"))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (run_id, "new-source", "exploded"))


def test_deleting_a_run_removes_its_children(conn: sqlite3.Connection) -> None:
    run_id = db.create_run(conn, "h")
    paper_id = _paper(conn)
    with conn:
        conn.execute(
            "INSERT INTO queries (run_id, text, origin) VALUES (?, 'q', 'user')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO source_runs (run_id, source, status) "
            "VALUES (?, 'arxiv', 'ok')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO hits (run_id, source, paper_id) VALUES (?, 'arxiv', ?)",
            (run_id, paper_id),
        )
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    for table in ("queries", "source_runs", "hits"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # papers are shared across runs and survive
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_upgrade_from_schema_v1_keeps_existing_data(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = db.connect(path)
    old.executescript(db._SCHEMA_V1)
    old.execute("INSERT INTO runs (project_hash) VALUES ('abc')")
    old.execute("INSERT INTO papers (title, doi) VALUES ('Old paper', '10.1/x')")
    old.commit()
    assert db.schema_version(old) == 1

    db.init_schema(old)
    assert db.schema_version(old) == db.SCHEMA_VERSION
    assert old.execute("SELECT project_hash, name FROM runs").fetchone()[:] == (
        "abc",
        None,
    )
    row = old.execute(
        "SELECT title, publication_status, work_type FROM papers"
    ).fetchone()
    assert tuple(row) == ("Old paper", None, None)
    assert "fetches" in _tables(old)
    old.close()


def test_run_names_are_unique_when_set(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("INSERT INTO runs (project_hash, name) VALUES ('a', 'first')")
        conn.execute("INSERT INTO runs (project_hash) VALUES ('b')")
        conn.execute("INSERT INTO runs (project_hash) VALUES ('c')")  # no name: fine
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO runs (project_hash, name) VALUES ('d', 'first')")


def test_fetches_are_unique_per_query_source_and_variant(
    conn: sqlite3.Connection,
) -> None:
    run_id = db.create_run(conn, "h")
    with conn:
        conn.execute(
            "INSERT INTO queries (run_id, text, origin) VALUES (?, 'q', 'user')",
            (run_id,),
        )
    query_id = conn.execute("SELECT id FROM queries").fetchone()[0]
    sql = (
        "INSERT INTO fetches (run_id, query_id, source, variant, status) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    with conn:
        conn.execute(sql, (run_id, query_id, "openalex", "title-abstract", "done"))
        conn.execute(sql, (run_id, query_id, "openalex", "fulltext", "done"))
        conn.execute(sql, (run_id, query_id, "arxiv", "", "rate_limited"))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (run_id, query_id, "arxiv", "", "done"))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql, (run_id, query_id, "s2", "", "exploded"))
