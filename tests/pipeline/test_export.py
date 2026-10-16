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
    assert "not scored" in text
    assert "| openalex | ok | 2 | 4 |" in text
    assert "`agents review` — arxiv: 1, openalex: 3" in text
    assert "top 2 of 3" in text
    assert "### 1. Agents That Review Code" in text
    assert "[DOI](https://doi.org/10.1000/a)" in text
    assert "Found by arxiv, openalex · 2 queries · best rank 1" in text
    assert "Human Oversight Of Agents" not in text  # beyond the top 2


# ---------------------------------------------------------------------------- scores

import json  # noqa: E402

from litsearch.pipeline.score import context_hash  # noqa: E402


def add_scores(
    conn: sqlite3.Connection,
    run_id: int,
    scores: dict[str, tuple[int, str]],
    *,
    basis: str = "abstract",
) -> None:
    """Score papers by title: ``{title: (score, reason)}`` for the run's query set."""
    queries = [r[0] for r in conn.execute("SELECT text FROM queries ORDER BY id")]
    ctx = context_hash(queries)
    with conn:
        for title, (score, reason) in scores.items():
            paper_id = conn.execute(
                "SELECT id FROM papers WHERE title = ?", (title,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO scores (paper_id, project_hash, input_hash, "
                "prompt_version, model, score, reason, summary, tags, matched_query, "
                "basis) "
                "VALUES (?, ?, 'i', 'v', 'm', ?, ?, ?, ?, ?, ?)",
                (
                    paper_id,
                    ctx,
                    score,
                    reason,
                    f"Summary of {title}. Two. Three.",
                    json.dumps(["tag one", "tag two"]),
                    "agents review" if score else None,
                    basis,
                ),
            )


@pytest.fixture
def scored(filled: tuple[sqlite3.Connection, int]) -> tuple[sqlite3.Connection, int]:
    conn, run_id = filled
    add_scores(
        conn,
        run_id,
        {
            "Trust In Automation": (5, "directly about trust"),
            "Agents That Review Code": (3, "partly relevant"),
            "Human Oversight Of Agents": (1, "only shares words | and a pipe"),
        },
    )
    return conn, run_id


def test_scored_candidates_come_best_score_first(
    scored: tuple[sqlite3.Connection, int],
) -> None:
    conn, run_id = scored
    candidates = load_candidates(conn, run_id)
    assert [(c.title, c.score) for c in candidates] == [
        ("Trust In Automation", 5.0),
        ("Agents That Review Code", 3.0),
        ("Human Oversight Of Agents", 1.0),
    ]
    top = candidates[0]
    assert top.reason == "directly about trust"
    assert top.matched_query == "agents review"
    assert top.summary.startswith("Summary of Trust In Automation")
    assert top.tags == ("tag one", "tag two")
    assert top.score_basis == "abstract"
    assert load_summary(conn, run_id).n_scored == 3


def test_unscored_papers_come_after_the_scored_ones(
    filled: tuple[sqlite3.Connection, int],
) -> None:
    conn, run_id = filled
    add_scores(conn, run_id, {"Human Oversight Of Agents": (2, "weak")})
    candidates = load_candidates(conn, run_id)
    assert candidates[0].title == "Human Oversight Of Agents"
    assert [c.score for c in candidates[1:]] == [None, None]
    assert load_summary(conn, run_id).n_scored == 1


def test_scores_of_another_query_set_are_ignored(
    scored: tuple[sqlite3.Connection, int],
) -> None:
    conn, _ = scored
    other = db.create_run(conn, "other")
    add_queries(conn, other, ["a completely different query"])
    paper_id = conn.execute("SELECT id FROM papers LIMIT 1").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO hits (run_id, source, paper_id, rank) VALUES (?, 'x', ?, 1)",
            (other, paper_id),
        )
    assert all(c.score is None for c in load_candidates(conn, other))


def test_the_csv_has_the_scoring_columns(
    scored: tuple[sqlite3.Connection, int], tmp_path: Path
) -> None:
    conn, run_id = scored
    path = tmp_path / "c.csv"
    write_csv(load_candidates(conn, run_id), path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["score"] for r in rows] == ["5", "3", "1"]
    assert rows[0]["matched_query"] == "agents review"
    assert rows[0]["reason"] == "directly about trust"
    assert rows[0]["tags"] == "tag one; tag two"
    assert rows[0]["score_basis"] == "abstract"
    assert rows[0]["summary"].startswith("Summary of Trust In Automation")


def test_the_markdown_groups_papers_by_score(
    scored: tuple[sqlite3.Connection, int], tmp_path: Path
) -> None:
    conn, run_id = scored
    path = tmp_path / "c.md"
    write_markdown(load_candidates(conn, run_id), load_summary(conn, run_id), path)
    text = path.read_text(encoding="utf-8")
    assert "Scored 3 of 3 papers from 0 to 5" in text
    assert "relative to the queries below" in text
    assert "## Relevant (score 4 or more): top 1 of 1" in text
    assert "### 1. Trust In Automation  `5/5`" in text
    assert "**Fits** `agents review`: directly about trust" in text
    assert "**Summary** (based on the abstract): Summary of Trust In Automation" in text
    assert "Tags: tag one, tag two" in text
    assert "## Possibly relevant (score 3)" in text
    assert "<summary>1 papers</summary>" in text
    assert "Agents That Review Code  `3/5`" in text
    assert "## Screened out (score below 3)" in text
    assert "| 1 | Human Oversight Of Agents | only shares words / and a pipe |" in text
    assert "## Not scored" not in text
    # the screened-out paper only appears in the table, not as a full entry
    assert "### 1. Human Oversight" not in text


def test_the_thresholds_and_the_top_limit_can_be_changed(
    scored: tuple[sqlite3.Connection, int], tmp_path: Path
) -> None:
    conn, run_id = scored
    path = tmp_path / "c.md"
    write_markdown(
        load_candidates(conn, run_id),
        load_summary(conn, run_id),
        path,
        min_score=3,
        maybe_score=1,
        top=1,
    )
    text = path.read_text(encoding="utf-8")
    assert "## Relevant (score 3 or more): top 1 of 2" in text
    assert "## Possibly relevant (score 1)" in text
    assert "Agents That Review Code" not in text.split("## Possibly relevant")[0]


def test_unscored_papers_are_pointed_out_when_some_were_scored(
    filled: tuple[sqlite3.Connection, int], tmp_path: Path
) -> None:
    conn, run_id = filled
    add_scores(conn, run_id, {"Agents That Review Code": (4, "relevant")})
    path = tmp_path / "c.md"
    write_markdown(load_candidates(conn, run_id), load_summary(conn, run_id), path)
    text = path.read_text(encoding="utf-8")
    assert "## Not scored (2)" in text
    assert f"litsearch score {run_id}" in text


def test_a_paper_judged_by_its_title_says_so(
    filled: tuple[sqlite3.Connection, int], tmp_path: Path
) -> None:
    conn, run_id = filled
    add_scores(conn, run_id, {"Agents That Review Code": (4, "fits")}, basis="title")
    path = tmp_path / "c.md"
    write_markdown(load_candidates(conn, run_id), load_summary(conn, run_id), path)
    assert "(based on the title only, no abstract)" in path.read_text(encoding="utf-8")


def add_model_scores(
    conn: sqlite3.Connection,
    model: str,
    prompt_version: str,
    scores: dict[str, int],
) -> None:
    queries = [r[0] for r in conn.execute("SELECT text FROM queries ORDER BY id")]
    ctx = context_hash(queries)
    with conn:
        for title, score in scores.items():
            paper_id = conn.execute(
                "SELECT id FROM papers WHERE title = ?", (title,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO scores (paper_id, project_hash, input_hash, "
                "prompt_version, model, score, reason, summary, tags) "
                "VALUES (?, ?, 'i', ?, ?, ?, 'r', 's', '[]')",
                (paper_id, ctx, prompt_version, model, score),
            )


def test_the_list_uses_one_model_never_a_mix(
    filled: tuple[sqlite3.Connection, int],
) -> None:
    conn, run_id = filled
    add_model_scores(
        conn,
        "old-model",
        "v1",
        {"Trust In Automation": 5, "Agents That Review Code": 5},
    )
    add_model_scores(
        conn, "new-model", "v1", {"Trust In Automation": 1}
    )  # scored later, only one
    candidates = {c.title: c for c in load_candidates(conn, run_id)}
    assert candidates["Trust In Automation"].score == 1.0
    assert (
        candidates["Agents That Review Code"].score is None
    )  # old-model score is ignored
    summary = load_summary(conn, run_id)
    assert summary.scored_by == "new-model, prompt v1"
    assert summary.n_scored == 1


def test_the_configured_model_is_preferred_over_a_newer_one(
    filled: tuple[sqlite3.Connection, int],
) -> None:
    conn, run_id = filled
    add_model_scores(
        conn, "mine", "v1", {"Trust In Automation": 5, "Agents That Review Code": 4}
    )
    add_model_scores(conn, "other", "v1", {"Trust In Automation": 1})
    mine = {c.title: c.score for c in load_candidates(conn, run_id, "mine")}
    assert mine["Trust In Automation"] == 5.0 and mine["Agents That Review Code"] == 4.0
    assert load_summary(conn, run_id, "mine").scored_by == "mine, prompt v1"
    # an unknown preference falls back to the most recent scores
    fallback = {c.title: c.score for c in load_candidates(conn, run_id, "nobody")}
    assert fallback["Trust In Automation"] == 1.0


def test_the_newest_prompt_version_of_a_model_is_used(
    filled: tuple[sqlite3.Connection, int],
) -> None:
    conn, run_id = filled
    add_model_scores(conn, "m", "v1", {"Trust In Automation": 2})
    add_model_scores(conn, "m", "v2", {"Trust In Automation": 4})
    assert load_candidates(conn, run_id, "m")[0].score == 4.0
    assert load_summary(conn, run_id, "m").scored_by == "m, prompt v2"


def test_the_markdown_says_which_model_scored(
    filled: tuple[sqlite3.Connection, int], tmp_path: Path
) -> None:
    conn, run_id = filled
    add_model_scores(conn, "gpt-4.1-nano", "score-v3", {"Trust In Automation": 5})
    path = tmp_path / "c.md"
    write_markdown(load_candidates(conn, run_id), load_summary(conn, run_id), path)
    assert "by `gpt-4.1-nano, prompt score-v3`" in path.read_text(encoding="utf-8")
