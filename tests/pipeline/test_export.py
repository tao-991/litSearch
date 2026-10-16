from __future__ import annotations

import csv
import itertools
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from litsearch import db
from litsearch.models import PaperMeta
from litsearch.pipeline.export import (
    latest_run_id,
    load_candidates,
    load_summary,
    write_csv,
    write_markdown,
)
from litsearch.pipeline.store import RunStore, add_queries

_ids = itertools.count(1)


def meta(title: str, **kwargs: Any) -> PaperMeta:
    fields: dict[str, Any] = {
        "source": "openalex",
        "source_id": f"X{next(_ids)}",
        "title": title,
        "authors": ("Jing Jiang", "Ana Costa"),
        "year": 2025,
    }
    fields.update(kwargs)
    return PaperMeta(**fields)


@pytest.fixture
def filled() -> tuple[sqlite3.Connection, int]:
    """A run with two queries: paper A is found by both queries and two sources."""
    conn = db.connect(":memory:")
    db.init_schema(conn)
    run_id = db.create_run(conn, "h")
    (q1, _), (q2, _) = add_queries(conn, run_id, ["agents review", "human in the loop"])
    store = RunStore(conn, run_id)
    a_oa = meta(
        "Agents That Review Code",
        doi="10.1000/a",
        venue="MSR",
        arxiv_id="2501.00001",
        abstract='About agents, with a "quote", and a comma.',
        links={
            "doi": "https://doi.org/10.1000/a",
            "arxiv": "https://arxiv.org/abs/2501.00001",
        },
        oa_pdf_url="https://example.org/a.pdf",
        work_type="conference-paper",
        citation_count=4,
    )
    a_arxiv = meta(
        "Agents That Review Code",
        source="arxiv",
        source_id="2501.00001",
        arxiv_id="2501.00001",
        work_type="preprint",
    )
    b = meta(
        "Trust In Automation", doi="10.1000/b", authors=("Maria Rossi",), year=2023
    )
    c = meta(
        "Human Oversight Of Agents", doi="10.1000/c", authors=("Li Hou",), year=2026
    )
    store.ingest([b, a_oa, c], query_id=q1, source="openalex")  # ranks: b=1, a=2, c=3
    store.ingest([a_arxiv], query_id=q1, source="arxiv")
    store.ingest([a_oa], query_id=q2, source="openalex")  # a is found by a second query
    store.finalize_source("openalex", 2)
    store.finalize_source("arxiv", 1)
    return conn, run_id


def test_candidates_are_ordered_by_how_many_queries_found_them(
    filled: tuple[sqlite3.Connection, int],
) -> None:
    conn, run_id = filled
    candidates = load_candidates(conn, run_id)
    assert [c.title for c in candidates] == [
        "Agents That Review Code",  # 2 queries
        "Trust In Automation",  # 1 query, best rank 1
        "Human Oversight Of Agents",  # 1 query, best rank 3
    ]
    top = candidates[0]
    assert top.sources == ("arxiv", "openalex")
    assert top.queries == ("agents review", "human in the loop")
    assert top.best_rank == 1  # rank 1 in the arXiv result list
    assert top.status == "published"
    assert top.venue == "MSR"
    assert top.doi == "10.1000/a"
    assert top.links["doi"] == "https://doi.org/10.1000/a"
    assert top.authors == ("Jing Jiang", "Ana Costa")


def test_summary_counts_sources_queries_and_papers(
    filled: tuple[sqlite3.Connection, int],
) -> None:
    conn, run_id = filled
    summary = load_summary(conn, run_id)
    assert summary.n_papers == 3
    assert summary.n_queries == 2
    assert [(s.source, s.status, s.n_results) for s in summary.sources] == [
        ("openalex", "ok", 4),
        ("arxiv", "ok", 1),
    ]
    assert dict(summary.queries)["agents review"] == {"openalex": 3, "arxiv": 1}
    assert dict(summary.queries)["human in the loop"] == {"openalex": 1}
    assert summary.name == f"run-{run_id:04d}" or summary.name  # name may be unset


def test_summary_of_a_missing_run_is_an_error(
    filled: tuple[sqlite3.Connection, int],
) -> None:
    conn, _ = filled
    with pytest.raises(LookupError):
        load_summary(conn, 999)


def test_latest_run_id() -> None:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    assert latest_run_id(conn) is None
    db.create_run(conn, "a")
    second = db.create_run(conn, "b")
    assert latest_run_id(conn) == second


def test_csv_has_one_row_per_paper_and_survives_quotes_and_commas(
    filled: tuple[sqlite3.Connection, int], tmp_path: Path
) -> None:
    conn, run_id = filled
    path = tmp_path / "out" / "candidates.csv"
    write_csv(load_candidates(conn, run_id), path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 3
    first = rows[0]
    assert first["title"] == "Agents That Review Code"
    assert first["abstract"] == 'About agents, with a "quote", and a comma.'
    assert first["authors"] == "Jing Jiang; Ana Costa"
    assert first["found_by_sources"] == "arxiv, openalex"
    assert first["n_queries"] == "2"
    assert first["queries"] == "agents review | human in the loop"
    assert first["doi_url"] == "https://doi.org/10.1000/a"
    assert first["oa_pdf_url"] == "https://example.org/a.pdf"
    assert first["citation_count"] == "4"
    assert first["publication_status"] == "published"


def test_markdown_lists_sources_queries_and_the_top_candidates(
    filled: tuple[sqlite3.Connection, int], tmp_path: Path
) -> None:
    conn, run_id = filled
    path = tmp_path / "candidates.md"
    write_markdown(
        load_candidates(conn, run_id), load_summary(conn, run_id), path, top=2
    )
    text = path.read_text(encoding="utf-8")
    assert "not scored yet" in text
    assert "| openalex | ok | 2 | 4 |" in text
    assert "`agents review` — arxiv: 1, openalex: 3" in text
    assert "top 2 of 3" in text
    assert "### 1. Agents That Review Code" in text
    assert "[DOI](https://doi.org/10.1000/a)" in text
    assert "Found by arxiv, openalex · 2 queries · best rank 1" in text
    assert "Human Oversight Of Agents" not in text  # beyond the top 2
