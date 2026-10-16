"""Readable output of a run: the candidate list as CSV and Markdown, and run summaries.

When the papers have been scored (0 to 5, relative to the run's queries) the list is
ordered by score and split into three groups: relevant (score at least ``min_score``),
possibly relevant (at least ``maybe_score``) and screened out. Without scores it is
ordered by how many of the run's queries found each paper, then by the best rank any
source gave it.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from litsearch import db
from litsearch.pipeline.score import MAX_SCORE, context_hash

DEFAULT_MIN_SCORE = 4  # at least this: the main list
DEFAULT_MAYBE_SCORE = 3  # at least this (and below min): "possibly relevant"
NO_RANK = 10**6


@dataclass(frozen=True, slots=True)
class Candidate:
    paper_id: int
    title: str
    authors: tuple[str, ...]
    year: int | None
    status: str
    venue: str | None
    doi: str | None
    arxiv_id: str | None
    citation_count: int | None
    abstract: str | None
    links: dict[str, str]
    oa_pdf_url: str | None
    sources: tuple[str, ...]
    queries: tuple[str, ...]
    best_rank: int
    score: float | None = None  # 0..5 from the model; None = not scored
    matched_query: str | None = None
    reason: str | None = None
    summary: str | None = None
    tags: tuple[str, ...] = ()
    score_basis: str | None = None  # "abstract" or "title"


@dataclass(frozen=True, slots=True)
class SourceLine:
    source: str
    status: str
    n_requests: int
    n_results: int
    error: str | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: int
    name: str
    status: str
    started_at: str
    finished_at: str | None
    n_queries: int
    n_papers: int
    n_scored: int
    sources: list[SourceLine]
    queries: list[tuple[str, dict[str, int]]]  # query text -> hits per source
    scored_by: str | None = None  # "model, prompt version" of the scores shown


def _run_scores(
    conn: sqlite3.Connection, run_id: int, prefer_model: str | None = None
) -> tuple[dict[int, sqlite3.Row], str | None]:
    """The scores of this run's query set from ONE model and prompt version, by paper,
    and a label saying which.

    The scores of ``prefer_model`` are used when there are any. Otherwise the most
    complete scoring wins (the model and prompt version that scored the most papers; a
    tie goes to the newest), so a model that was tried on a few papers does not hide a
    complete scoring. Scores of other models or prompt versions are ignored, so the list
    is never a mix.
    """
    queries = [
        r["text"]
        for r in conn.execute(
            "SELECT text FROM queries WHERE run_id = ? ORDER BY id", (run_id,)
        )
    ]
    if not queries:
        return {}, None
    ctx = context_hash(queries)
    newest = None
    if prefer_model:
        newest = conn.execute(
            "SELECT model, prompt_version FROM scores WHERE project_hash = ? "
            "AND model = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (ctx, prefer_model),
        ).fetchone()
    if newest is None:
        newest = conn.execute(
            "SELECT model, prompt_version, COUNT(*) AS n, MAX(created_at) AS latest "
            "FROM scores WHERE project_hash = ? GROUP BY model, prompt_version "
            "ORDER BY n DESC, latest DESC, MAX(rowid) DESC LIMIT 1",
            (ctx,),
        ).fetchone()
    if newest is None:
        return {}, None
    rows = {
        r["paper_id"]: r
        for r in conn.execute(
            "SELECT * FROM scores WHERE project_hash = ? AND model = ? "
            "AND prompt_version = ?",
            (ctx, newest["model"], newest["prompt_version"]),
        )
    }
    return rows, f"{newest['model']}, prompt {newest['prompt_version']}"


def load_candidates(
    conn: sqlite3.Connection, run_id: int, prefer_model: str | None = None
) -> list[Candidate]:
    """Every paper the run found, best first."""
    papers = {
        r["id"]: r
        for r in conn.execute(
            "SELECT * FROM papers WHERE id IN "
            "(SELECT paper_id FROM hits WHERE run_id = ?)",
            (run_id,),
        )
    }
    sources: dict[int, set[str]] = {}
    queries: dict[int, dict[str, None]] = {}  # dict keeps first-seen order
    best: dict[int, int] = {}
    for h in conn.execute(
        "SELECT h.paper_id, h.source, h.rank, q.text FROM hits h "
        "LEFT JOIN queries q ON q.id = h.query_id WHERE h.run_id = ? ORDER BY h.id",
        (run_id,),
    ):
        pid = h["paper_id"]
        sources.setdefault(pid, set()).add(h["source"])
        if h["text"]:
            queries.setdefault(pid, {})[h["text"]] = None
        if h["rank"] is not None:
            best[pid] = min(best.get(pid, h["rank"]), h["rank"])
    scores, _ = _run_scores(conn, run_id, prefer_model)

    found = []
    for pid, row in papers.items():
        score = scores.get(pid)
        found.append(
            Candidate(
                paper_id=pid,
                title=row["title"],
                authors=tuple(db.loads_json(row["authors"]) or ()),
                year=row["year"],
                status=row["publication_status"] or "unknown",
                venue=row["venue"],
                doi=row["doi"],
                arxiv_id=row["arxiv_id"],
                citation_count=row["citation_count"],
                abstract=row["abstract"],
                links=db.loads_json(row["links"]) or {},
                oa_pdf_url=row["oa_pdf_url"],
                sources=tuple(sorted(sources.get(pid, ()))),
                queries=tuple(queries.get(pid, {})),
                best_rank=best.get(pid, NO_RANK),
                score=None if score is None else float(score["score"]),
                matched_query=None if score is None else score["matched_query"],
                reason=None if score is None else score["reason"],
                summary=None if score is None else score["summary"],
                tags=() if score is None else tuple(db.loads_json(score["tags"]) or ()),
                score_basis=None if score is None else score["basis"],
            )
        )
    found.sort(
        key=lambda c: (
            c.score is None,
            -(c.score or 0),
            -len(c.queries),
            c.best_rank,
            -(c.year or 0),
            c.paper_id,
        )
    )
    return found


def load_summary(
    conn: sqlite3.Connection, run_id: int, prefer_model: str | None = None
) -> RunSummary:
    run = db.get_run(conn, run_id)
    if run is None:
        raise LookupError(f"no run with id {run_id}")
    source_rows = conn.execute(
        "SELECT source, status, n_requests, n_results, error FROM source_runs "
        "WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    per_query: dict[str, dict[str, int]] = {}
    for q in conn.execute(
        "SELECT id, text FROM queries WHERE run_id = ? ORDER BY id", (run_id,)
    ):
        counts = {
            r["source"]: r["n"]
            for r in conn.execute(
                "SELECT source, COUNT(*) AS n FROM hits "
                "WHERE run_id = ? AND query_id = ? GROUP BY source",
                (run_id, q["id"]),
            )
        }
        per_query[q["text"]] = counts
    paper_ids = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT paper_id FROM hits WHERE run_id = ?", (run_id,)
        )
    }
    score_rows, score_label = _run_scores(conn, run_id, prefer_model)
    scored = paper_ids & set(score_rows)
    return RunSummary(
        run_id=run_id,
        name=run["name"] or f"run-{run_id:04d}",
        status=run["status"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        n_queries=len(per_query),
        n_papers=len(paper_ids),
        n_scored=len(scored),
        sources=[SourceLine(**dict(r)) for r in source_rows],
        queries=list(per_query.items()),
        scored_by=score_label if scored else None,
    )


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(id) FROM runs").fetchone()
    return None if row[0] is None else int(row[0])


def write_csv(candidates: list[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "paper_id",
                "score",
                "title",
                "authors",
                "year",
                "publication_status",
                "venue",
                "doi",
                "arxiv_id",
                "matched_query",
                "reason",
                "summary",
                "tags",
                "score_basis",
                "found_by_sources",
                "n_queries",
                "best_rank",
                "queries",
                "doi_url",
                "arxiv_url",
                "oa_pdf_url",
                "landing_page_url",
                "openalex_url",
                "citation_count",
                "abstract",
            ]
        )
        for c in candidates:
            writer.writerow(
                [
                    c.paper_id,
                    "" if c.score is None else f"{c.score:g}",
                    c.title,
                    "; ".join(c.authors),
                    c.year or "",
                    c.status,
                    c.venue or "",
                    c.doi or "",
                    c.arxiv_id or "",
                    c.matched_query or "",
                    c.reason or "",
                    c.summary or "",
                    "; ".join(c.tags),
                    c.score_basis or "",
                    ", ".join(c.sources),
                    len(c.queries),
                    c.best_rank if c.best_rank < NO_RANK else "",
                    " | ".join(c.queries),
                    c.links.get("doi", ""),
                    c.links.get("arxiv", ""),
                    c.oa_pdf_url or "",
                    c.links.get("landing_page", ""),
                    c.links.get("openalex", ""),
                    "" if c.citation_count is None else c.citation_count,
                    c.abstract or "",
                ]
            )


@dataclass(frozen=True, slots=True)
class QueryGroup:
    """The papers that fit one query best, split by score."""

    query: str | None  # None = papers that fit no query
    n_found: int  # results the sources returned for this query (all sources)
    relevant: list[Candidate]  # score at least min_score
    maybe: list[Candidate]  # score from maybe_score up to min_score


@dataclass(frozen=True, slots=True)
class Grouped:
    groups: list[QueryGroup]  # one per query of the run, in the order of the run
    screened_out: list[Candidate]  # scored below maybe_score (or fitting no query)
    unscored: list[Candidate]


def group_by_query(
    candidates: list[Candidate],
    summary: RunSummary,
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    maybe_score: int = DEFAULT_MAYBE_SCORE,
) -> Grouped:
    """Group scored papers by the query they fit best, best score first.

    Every query of the run gets a group (a query without relevant papers is worth
    seeing). A paper appears in exactly one place.
    """
    queries = [text for text, _ in summary.queries]
    found_per_query = {text: sum(counts.values()) for text, counts in summary.queries}
    relevant: dict[str, list[Candidate]] = {q: [] for q in queries}
    maybe: dict[str, list[Candidate]] = {q: [] for q in queries}
    screened: list[Candidate] = []
    unscored: list[Candidate] = []
    for c in candidates:  # candidates are already ordered best first
        if c.score is None:
            unscored.append(c)
        elif c.matched_query in relevant and c.score >= min_score:
            relevant[c.matched_query].append(c)
        elif c.matched_query in maybe and c.score >= maybe_score:
            maybe[c.matched_query].append(c)
        else:
            screened.append(c)
    groups = [
        QueryGroup(q, found_per_query.get(q, 0), relevant[q], maybe[q]) for q in queries
    ]
    return Grouped(groups, screened, unscored)


def _link_line(c: Candidate) -> str:
    parts = [str(c.year or "year ?"), c.venue or "venue ?", c.status]
    for label, url in (
        ("DOI", c.links.get("doi")),
        ("arXiv", c.links.get("arxiv")),
        ("PDF", c.oa_pdf_url),
        ("page", c.links.get("landing_page")),
    ):
        if url and url.lower().startswith(("http://", "https://")):
            parts.append(f"[{label}]({url})")
    return " · ".join(parts)


def _found_by(c: Candidate) -> str:
    count = len(c.queries)
    noun = "query" if count == 1 else "queries"
    text = f"Found by {', '.join(c.sources)} · {count} {noun}"
    if c.best_rank < NO_RANK:
        text += f" · best rank {c.best_rank}"
    return text


def _scored_entry(c: Candidate, *, heading: str, full: bool) -> list[str]:
    """One paper of a scored list; ``full`` adds authors and where it was found."""
    lines = [f"{heading} {c.title}  `{c.score:g}/{MAX_SCORE}`", _link_line(c), ""]
    if full:
        authors = ", ".join(c.authors[:4]) + (" et al." if len(c.authors) > 4 else "")
        if authors:
            lines += [authors, ""]
    if c.reason:
        lines.append(f"**Fits:** {c.reason}")
    if c.summary:
        basis = (
            "based on the title only, no abstract"
            if c.score_basis == "title"
            else "based on the abstract"
        )
        lines.append(f"**Summary** ({basis}): {c.summary}")
    if c.tags:
        lines.append("Tags: " + ", ".join(c.tags))
    if full:
        lines.append(_found_by(c))
    lines.append("")
    return lines


def _queries_phrase(n: int) -> str:
    return f"{n} query" if n == 1 else f"{n} queries"


def write_markdown(
    candidates: list[Candidate],
    summary: RunSummary,
    path: Path,
    *,
    top: int = 50,
    min_score: int = DEFAULT_MIN_SCORE,
    maybe_score: int = DEFAULT_MAYBE_SCORE,
) -> None:
    """The list as Markdown: papers grouped by the query they fit best, best score
    first. ``top`` limits the relevant papers listed per query."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scored = [c for c in candidates if c.score is not None]
    n_queries = summary.n_queries
    lines = [
        f"# {summary.name}",
        "",
        f"Status: **{summary.status}** · {_queries_phrase(n_queries)} · "
        f"{summary.n_papers} distinct papers found",
        "",
    ]
    if scored:
        lines += [
            f"Scored {len(scored)} of {len(candidates)} papers from 0 to {MAX_SCORE} "
            f"by `{summary.scored_by}`, relative to the queries below (a paper is "
            "relevant if it addresses at least one of them). Summaries and reasons "
            "are written by a model from the title and abstract only; links, DOIs, "
            "authors and years come from the data sources.",
            "",
        ]
    else:
        lines += [
            "These candidates are **not scored**: they are ordered by how many of the "
            "queries found them, then by their best rank in a source's result list.",
            "",
        ]
    lines += [
        "## Sources",
        "",
        "| Source | Status | Requests | Results | Note |",
        "|---|---|---|---|---|",
    ]
    for s in summary.sources:
        lines.append(
            f"| {s.source} | {s.status} | {s.n_requests} | {s.n_results} "
            f"| {s.error or ''} |"
        )

    if not scored:
        lines += ["", "## Queries", ""]
        for text, counts in summary.queries:
            found = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
            lines.append(f"- `{text}` — {found or 'no results'}")
        lines += _unscored_section(candidates, top)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    grouped = group_by_query(
        candidates, summary, min_score=min_score, maybe_score=maybe_score
    )
    for number, g in enumerate(grouped.groups, start=1):
        shown = g.relevant[:top]
        lines += [
            "",
            f"## {number}. `{g.query}`",
            "",
            f"{g.n_found} results · **{len(g.relevant)} relevant** (score {min_score}+)"
            f" · {len(g.maybe)} possibly relevant (score {maybe_score})",
            "",
        ]
        if not g.relevant:
            lines += ["No relevant paper fits this query best.", ""]
        for c in shown:
            lines += _scored_entry(c, heading="###", full=True)
        if len(g.relevant) > len(shown):
            lines += [f"... {len(g.relevant) - len(shown)} more in the CSV.", ""]
        if g.maybe:
            lines += [
                f"<details><summary>Possibly relevant (score {maybe_score}): "
                f"{len(g.maybe)}</summary>",
                "",
            ]
            for c in g.maybe:
                lines += _scored_entry(c, heading="####", full=False)
            lines += ["</details>", ""]
    if grouped.unscored:
        lines += [
            "",
            f"## Not scored ({len(grouped.unscored)})",
            "",
            "These papers were found but not scored (a limit was reached or the "
            f"model failed). Run `litsearch score {summary.run_id}` to score them; "
            "they are listed in the CSV.",
            "",
        ]
    if grouped.screened_out:
        lines += [
            "",
            f"## Screened out (score below {maybe_score} or fitting no query)",
            "",
            f"<details><summary>{len(grouped.screened_out)} papers</summary>",
            "",
            "| Score | Title | Reason |",
            "|---|---|---|",
        ]
        for c in grouped.screened_out[:200]:
            lines.append(
                f"| {c.score:g} | {c.title.replace('|', '/')} "
                f"| {(c.reason or '').replace('|', '/')} |"
            )
        if len(grouped.screened_out) > 200:
            lines.append(
                f"| | ... {len(grouped.screened_out) - 200} more in the CSV | |"
            )
        lines += ["", "</details>", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _unscored_section(candidates: list[Candidate], top: int) -> list[str]:
    shown = candidates[:top]
    lines = [
        "",
        f"## Candidates (top {len(shown)} of {len(candidates)}; all are in the CSV)",
        "",
    ]
    for i, c in enumerate(shown, start=1):
        authors = ", ".join(c.authors[:4]) + (" et al." if len(c.authors) > 4 else "")
        lines += [f"### {i}. {c.title}", _link_line(c), "", authors, _found_by(c), ""]
    return lines
