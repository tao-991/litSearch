"""Readable output of a run: the candidate list as CSV and Markdown, and run summaries.

The candidate list is not scored yet (scoring comes in a later step), so it is ordered
by how many of the run's queries found each paper, then by the best rank any source gave
it.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from litsearch import db

LINK_COLUMNS = ("doi", "arxiv", "landing_page", "openalex", "semantic_scholar")


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
    sources: list[SourceLine]
    queries: list[tuple[str, dict[str, int]]]  # query text -> hits per source


def load_candidates(conn: sqlite3.Connection, run_id: int) -> list[Candidate]:
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

    found = [
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
            best_rank=best.get(pid, 10**6),
        )
        for pid, row in papers.items()
    ]
    found.sort(key=lambda c: (-len(c.queries), c.best_rank, -(c.year or 0), c.paper_id))
    return found


def load_summary(conn: sqlite3.Connection, run_id: int) -> RunSummary:
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
    n_papers = conn.execute(
        "SELECT COUNT(DISTINCT paper_id) FROM hits WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    return RunSummary(
        run_id=run_id,
        name=run["name"] or f"run-{run_id:04d}",
        status=run["status"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        n_queries=len(per_query),
        n_papers=int(n_papers),
        sources=[SourceLine(**dict(r)) for r in source_rows],
        queries=list(per_query.items()),
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
                "title",
                "authors",
                "year",
                "publication_status",
                "venue",
                "doi",
                "arxiv_id",
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
                    c.title,
                    "; ".join(c.authors),
                    c.year or "",
                    c.status,
                    c.venue or "",
                    c.doi or "",
                    c.arxiv_id or "",
                    ", ".join(c.sources),
                    len(c.queries),
                    c.best_rank if c.best_rank < 10**6 else "",
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


def _link_line(c: Candidate) -> str:
    parts = [str(c.year or "year ?"), c.venue or "venue ?", c.status]
    for label, url in (
        ("DOI", c.links.get("doi")),
        ("arXiv", c.links.get("arxiv")),
        ("PDF", c.oa_pdf_url),
        ("page", c.links.get("landing_page")),
    ):
        if url:
            parts.append(f"[{label}]({url})")
    return " · ".join(parts)


def write_markdown(
    candidates: list[Candidate], summary: RunSummary, path: Path, *, top: int = 50
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {summary.name}",
        "",
        f"Status: **{summary.status}** · {summary.n_queries} queries · "
        f"{summary.n_papers} distinct papers found",
        "",
        "These candidates are **not scored yet**: they are ordered by how many of the "
        "queries found them, then by their best rank in a source's result list.",
        "",
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
    lines += ["", "## Queries", ""]
    for text, counts in summary.queries:
        found = (
            ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no results"
        )
        lines.append(f"- `{text}` — {found}")
    shown = candidates[:top]
    lines += [
        "",
        f"## Candidates (top {len(shown)} of {len(candidates)}; all are in the CSV)",
        "",
    ]
    for i, c in enumerate(shown, start=1):
        authors = ", ".join(c.authors[:4]) + (" et al." if len(c.authors) > 4 else "")
        lines += [
            f"### {i}. {c.title}",
            _link_line(c),
            "",
            f"{authors}" if authors else "",
            f"Found by {', '.join(c.sources)} · {len(c.queries)} "
            f"{'query' if len(c.queries) == 1 else 'queries'}"
            + (f" · best rank {c.best_rank}" if c.best_rank < 10**6 else ""),
            "",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
