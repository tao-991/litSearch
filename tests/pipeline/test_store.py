from __future__ import annotations

import itertools
import json
import sqlite3
from typing import Any

import pytest

from litsearch import db
from litsearch.models import PaperMeta
from litsearch.pipeline.store import RunStore, add_queries, load_queries

_ids = itertools.count(1)


def meta(title: str = "Agents That Review Code", **kwargs: Any) -> PaperMeta:
    fields: dict[str, Any] = {
        "source": "openalex",
        "source_id": f"W{next(_ids)}",
        "title": title,
        "authors": ("Jing Jiang", "Ana Costa"),
        "year": 2025,
    }
    fields.update(kwargs)
    return PaperMeta(**fields)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def new_run(conn: sqlite3.Connection, *queries: str) -> tuple[int, list[int]]:
    run_id = db.create_run(conn, "hash")
    stored = add_queries(conn, run_id, queries or ("q",))
    return run_id, [qid for qid, _ in stored]


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ------------------------------------------------------------------------ queries


def test_queries_are_normalized_deduplicated_and_ordered(
    conn: sqlite3.Connection,
) -> None:
    run_id = db.create_run(conn, "h")
    stored = add_queries(
        conn, run_id, ["  agents   review ", "", "agents review", "trust"]
    )
    assert [t for _, t in stored] == ["agents review", "trust"]
    assert load_queries(conn, run_id) == stored


# ----------------------------------------------------------------------- ingesting


def test_ingest_stores_paper_record_hit_and_fetch(conn: sqlite3.Connection) -> None:
    run_id, (qid,) = new_run(conn)
    store = RunStore(conn, run_id)
    paper = meta(
        doi="10.1000/x",
        venue="MSR",
        abstract="An abstract.",
        raw={"source": "payload"},
        links={"doi": "https://doi.org/10.1000/x"},
        work_type="conference-paper",
    )
    result = store.ingest([paper], query_id=qid, source="openalex")
    assert (result.n_results, result.n_new) == (1, 1)

    row = conn.execute("SELECT * FROM papers").fetchone()
    assert row["title"] == paper.title
    assert row["doi"] == "10.1000/x"
    assert row["venue"] == "MSR"
    assert row["publication_status"] == "published"
    assert row["state"] == "candidate"
    assert db.loads_json(row["authors"]) == ["Jing Jiang", "Ana Costa"]
    assert db.loads_json(row["links"]) == {"doi": "https://doi.org/10.1000/x"}

    record = conn.execute("SELECT * FROM paper_records").fetchone()
    assert (record["source"], record["source_id"]) == ("openalex", paper.source_id)
    assert json.loads(record["raw"]) == {"source": "payload"}
    assert json.loads(record["meta"])["title"] == paper.title

    hit = conn.execute("SELECT * FROM hits").fetchone()
    assert (hit["run_id"], hit["query_id"], hit["source"]) == (run_id, qid, "openalex")
    assert (hit["paper_id"], hit["rank"], hit["round"]) == (row["id"], 1, 0)

    fetch = conn.execute("SELECT * FROM fetches").fetchone()
    assert (fetch["status"], fetch["n_results"], fetch["variant"]) == ("done", 1, "")


def test_ranks_follow_the_position_in_the_page_plus_offset(
    conn: sqlite3.Connection,
) -> None:
    run_id, (qid,) = new_run(conn)
    store = RunStore(conn, run_id)
    store.ingest(
        [
            meta("First paper on agents", doi="10.1/a", authors=("A One",)),
            meta("Second paper on testing", doi="10.1/b", authors=("B Two",)),
        ],
        query_id=qid,
        source="openalex",
        offset=20,
    )
    ranks = [r[0] for r in conn.execute("SELECT rank FROM hits ORDER BY rank")]
    assert ranks == [21, 22]


def test_the_same_paper_from_two_sources_is_one_row_with_two_records(
    conn: sqlite3.Connection,
) -> None:
    run_id, (qid,) = new_run(conn)
    store = RunStore(conn, run_id)
    preprint = meta(
        source="arxiv",
        source_id="2501.00001",
        arxiv_id="2501.00001",
        work_type="preprint",
        abstract="Short.",
    )
    published = meta(
        arxiv_id="2501.00001",
        doi="10.1109/msr.2025.1",
        venue="MSR",
        work_type="conference-paper",
        abstract="A considerably longer abstract.",
    )
    first = store.ingest([preprint], query_id=qid, source="arxiv")
    second = store.ingest([published], query_id=qid, source="openalex")
    assert (first.n_new, second.n_new) == (1, 0)
    assert count(conn, "papers") == 1
    assert count(conn, "paper_records") == 2
    assert count(conn, "hits") == 2
    row = conn.execute("SELECT * FROM papers").fetchone()
    assert row["doi"] == "10.1109/msr.2025.1"  # refreshed from the better record
    assert row["venue"] == "MSR"
    assert row["publication_status"] == "published"
    assert row["abstract"] == "A considerably longer abstract."


def test_the_same_record_found_by_two_queries_is_stored_once_with_two_hits(
    conn: sqlite3.Connection,
) -> None:
    run_id, (q1, q2) = new_run(conn, "first", "second")
    store = RunStore(conn, run_id)
    paper = meta(doi="10.1000/x")
    store.ingest([paper], query_id=q1, source="openalex")
    store.ingest([paper], query_id=q2, source="openalex")
    assert count(conn, "papers") == 1
    assert count(conn, "paper_records") == 1
    assert count(conn, "hits") == 2


def test_a_later_run_recognises_papers_from_an_earlier_run(
    conn: sqlite3.Connection,
) -> None:
    run1, (q1,) = new_run(conn)
    RunStore(conn, run1).ingest(
        [meta(doi="10.1000/x", arxiv_id="2501.00001")], query_id=q1, source="openalex"
    )
    run2, (q2,) = new_run(conn)
    store2 = RunStore(conn, run2)  # rebuilds its state from the stored records
    again = meta(
        source="arxiv",
        source_id="2501.00001",
        arxiv_id="2501.00001",
        work_type="preprint",
    )
    result = store2.ingest([again], query_id=q2, source="arxiv")
    assert result.n_new == 0
    assert count(conn, "papers") == 1
    assert count(conn, "paper_records") == 2
    hit_runs = {r[0] for r in conn.execute("SELECT run_id FROM hits")}
    assert hit_runs == {run1, run2}


def test_a_record_that_bridges_two_stored_papers_merges_their_rows(
    conn: sqlite3.Connection,
) -> None:
    run_id, (qid,) = new_run(conn)
    store = RunStore(conn, run_id)
    only_doi = meta("Alpha Beta Gamma Delta", doi="10.1000/x", authors=("A One",))
    only_arxiv = meta(
        "Entirely Other Words Here",
        source="arxiv",
        source_id="2601.00001",
        arxiv_id="2601.00001",
        authors=("B Two",),
    )
    store.ingest([only_doi], query_id=qid, source="openalex")
    store.ingest([only_arxiv], query_id=qid, source="arxiv")
    assert count(conn, "papers") == 2
    ids = [r[0] for r in conn.execute("SELECT id FROM papers ORDER BY id")]
    with conn:  # a score and an edge hang on the paper that will be dropped
        conn.execute(
            "INSERT INTO scores (paper_id, project_hash, input_hash, prompt_version, "
            "model, score) VALUES (?, 'p', 'i', 'v', 'm', 7)",
            (ids[1],),
        )
        conn.execute(
            "INSERT INTO edges (src_paper_id, dst_paper_id) VALUES (?, ?)",
            (ids[1], ids[0]),
        )

    bridge = meta("Whatever", doi="10.1000/x", arxiv_id="2601.00001", authors=())
    result = store.ingest([bridge], query_id=qid, source="openalex")
    assert result.n_new == 0
    assert count(conn, "papers") == 1
    survivor = conn.execute("SELECT * FROM papers").fetchone()
    assert survivor["id"] == ids[0]
    assert survivor["doi"] == "10.1000/x"
    assert survivor["arxiv_id"] == "2601.00001"
    assert count(conn, "paper_records") == 3
    assert {r[0] for r in conn.execute("SELECT paper_id FROM paper_records")} == {
        ids[0]
    }
    assert count(conn, "hits") == 3  # nothing lost: two earlier hits plus the bridge's
    assert {r[0] for r in conn.execute("SELECT paper_id FROM hits")} == {ids[0]}
    assert conn.execute("SELECT paper_id FROM scores").fetchone()[0] == ids[0]
    assert count(conn, "edges") == 0  # the edge became a self-loop and was dropped


# ------------------------------------------------------------------ fetch bookkeeping


def test_failed_fetches_are_logged_and_a_retry_replaces_them(
    conn: sqlite3.Connection,
) -> None:
    run_id, (qid,) = new_run(conn)
    store = RunStore(conn, run_id)
    store.record_failed_fetch(
        qid, "arxiv", "", "rate_limited", "HTTP 429 after retries"
    )
    assert store.done_fetches() == set()
    row = conn.execute("SELECT status, error FROM fetches").fetchone()
    assert (row["status"], row["error"]) == ("rate_limited", "HTTP 429 after retries")

    store.ingest([meta(doi="10.1/a")], query_id=qid, source="arxiv")
    assert store.done_fetches() == {(qid, "arxiv", "")}
    assert count(conn, "fetches") == 1  # replaced, not duplicated
    assert conn.execute("SELECT error FROM fetches").fetchone()[0] is None


def test_variants_are_separate_fetches(conn: sqlite3.Connection) -> None:
    run_id, (qid,) = new_run(conn)
    store = RunStore(conn, run_id)
    store.ingest(
        [meta(doi="10.1/a")], query_id=qid, source="openalex", variant="fulltext"
    )
    store.ingest(
        [meta(doi="10.1/b", authors=("B Two",), title="Another title entirely")],
        query_id=qid,
        source="openalex",
        variant="title-abstract",
    )
    assert store.done_fetches() == {
        (qid, "openalex", "fulltext"),
        (qid, "openalex", "title-abstract"),
    }


def test_source_status_is_ok_when_every_fetch_succeeded(
    conn: sqlite3.Connection,
) -> None:
    run_id, (qid,) = new_run(conn)
    store = RunStore(conn, run_id)
    store.ingest(
        [meta(doi="10.1/a"), meta("Other", doi="10.1/b", authors=("X Y",))],
        query_id=qid,
        source="openalex",
    )
    assert store.finalize_source("openalex", n_requests=1) == "ok"
    row = conn.execute("SELECT * FROM source_runs").fetchone()
    assert (row["status"], row["n_requests"], row["n_results"], row["error"]) == (
        "ok",
        1,
        2,
        None,
    )


def test_source_status_reports_the_most_serious_failure(
    conn: sqlite3.Connection,
) -> None:
    run_id, (q1, q2, q3) = new_run(conn, "one", "two", "three")
    store = RunStore(conn, run_id)
    store.ingest([meta(doi="10.1/a")], query_id=q1, source="s2")
    store.record_failed_fetch(q2, "s2", "", "skipped", "skipped after failures")
    store.record_failed_fetch(q3, "s2", "", "rate_limited", "HTTP 429 after retries")
    assert store.finalize_source("s2", n_requests=9) == "rate_limited"
    row = conn.execute("SELECT * FROM source_runs").fetchone()
    assert row["n_results"] == 1
    assert "2 of 3 fetches failed" in row["error"]
    assert "HTTP 429" in row["error"]


def test_blocked_outranks_rate_limited(conn: sqlite3.Connection) -> None:
    run_id, (q1, q2) = new_run(conn, "one", "two")
    store = RunStore(conn, run_id)
    store.record_failed_fetch(q1, "s2", "", "rate_limited", "429")
    store.record_failed_fetch(q2, "s2", "", "blocked", "403")
    assert store.finalize_source("s2", 2) == "blocked"


def test_request_counts_accumulate_across_resumed_sessions(
    conn: sqlite3.Connection,
) -> None:
    run_id, (q1, q2) = new_run(conn, "one", "two")
    store = RunStore(conn, run_id)
    store.ingest([meta(doi="10.1/a")], query_id=q1, source="arxiv")
    store.finalize_source("arxiv", n_requests=1)
    store.ingest(
        [meta("Other", doi="10.1/b", authors=("X Y",))], query_id=q2, source="arxiv"
    )
    store.finalize_source("arxiv", n_requests=1)
    row = conn.execute("SELECT * FROM source_runs").fetchone()
    assert (row["n_requests"], row["n_results"]) == (2, 2)
    assert count(conn, "source_runs") == 1
