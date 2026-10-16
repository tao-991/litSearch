"""Persistence of retrieval results (SPEC §5).

``RunStore`` writes what the collectors return: de-duplicated papers (shared by all
runs), every source record, hits (which query found which paper at which rank), and a
fetch log that makes a run resumable.

De-duplication state is rebuilt from the stored source records when a store is
created, so a paper found in an earlier run is recognised again in a later one. If two
papers that were stored separately turn out to be the same (a new record bridges them),
their rows are merged.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from litsearch.db import dumps_json
from litsearch.models import MergedPaper, PaperMeta, paper_from_dict, paper_to_dict
from litsearch.pipeline.dedup import Deduplicator

# When some fetches of a source failed, report the most serious failure kind.
_FAILURE_PRECEDENCE = ("blocked", "rate_limited", "error", "skipped")


@dataclass(frozen=True, slots=True)
class IngestResult:
    n_results: int  # records the source returned
    n_new: int  # papers not seen before (in any run)


def add_queries(
    conn: sqlite3.Connection, run_id: int, texts: Sequence[str], origin: str = "user"
) -> list[tuple[int, str]]:
    """Store the distinct, non-empty queries of a run; returns (id, text) in order."""
    stored: list[tuple[int, str]] = []
    seen: set[str] = set()
    with conn:
        for raw in texts:
            text = " ".join(raw.split())
            if not text or text in seen:
                continue
            seen.add(text)
            cur = conn.execute(
                "INSERT INTO queries (run_id, text, origin) VALUES (?, ?, ?)",
                (run_id, text, origin),
            )
            assert cur.lastrowid is not None
            stored.append((cur.lastrowid, text))
    return stored


def load_queries(conn: sqlite3.Connection, run_id: int) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, text FROM queries WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    return [(r["id"], r["text"]) for r in rows]


def done_fetch_keys(conn: sqlite3.Connection, run_id: int) -> set[tuple[int, str, str]]:
    """(query_id, source, variant) of every fetch of a run that completed."""
    rows = conn.execute(
        "SELECT query_id, source, variant FROM fetches "
        "WHERE run_id = ? AND status = 'done'",
        (run_id,),
    ).fetchall()
    return {(r["query_id"], r["source"], r["variant"]) for r in rows}


def _paper_values(paper: MergedPaper) -> tuple[object, ...]:
    return (
        paper.title,
        paper.abstract,
        dumps_json(list(paper.authors)),
        paper.year,
        paper.date,
        paper.venue,
        paper.doi,
        paper.arxiv_id,
        paper.openalex_id,
        paper.s2_id,
        paper.citation_count,
        dumps_json(paper.links),
        paper.oa_pdf_url,
        paper.status,
        paper.work_type,
    )


_PAPER_COLUMNS = (
    "title, abstract, authors, year, date, venue, doi, arxiv_id, openalex_id, s2_id, "
    "citation_count, links, oa_pdf_url, publication_status, work_type"
)


class RunStore:
    """Writes the results of one run and keeps the de-duplication state in step."""

    def __init__(self, conn: sqlite3.Connection, run_id: int) -> None:
        self._conn = conn
        self.run_id = run_id
        self._gid_paper: dict[int, int] = {}  # de-duplication group -> papers.id
        self._dirty: set[int] = set()  # groups whose paper row needs refreshing
        self.dedup = Deduplicator(on_join=self._on_join)
        self._seed()

    # ------------------------------------------------------------ rebuilding state

    def _seed(self) -> None:
        rows = self._conn.execute(
            "SELECT paper_id, meta FROM paper_records "
            "WHERE meta IS NOT NULL ORDER BY id"
        ).fetchall()
        with self._conn:
            for row in rows:
                gid = self.dedup.add(paper_from_dict(json.loads(row["meta"])))
                self._claim(gid, row["paper_id"])
            self._flush()

    def _claim(self, gid: int, paper_id: int) -> None:
        """Record that group ``gid`` is stored as ``paper_id`` (merging rows if two)."""
        current = self._gid_paper.get(gid)
        if current is None:
            self._gid_paper[gid] = paper_id
        elif current != paper_id:
            keep, drop = sorted((current, paper_id))
            self._merge_rows(keep, drop)
            self._gid_paper[gid] = keep
            self._dirty.add(gid)

    def _on_join(self, kept: int, dropped: int) -> None:
        paper_id = self._gid_paper.pop(dropped, None)
        if dropped in self._dirty:
            self._dirty.discard(dropped)
            self._dirty.add(kept)
        if paper_id is not None:
            self._claim(kept, paper_id)

    def _merge_rows(self, keep: int, drop: int) -> None:
        """Fold paper row ``drop`` into ``keep`` (two rows of one paper)."""
        c = self._conn
        c.execute(
            "UPDATE paper_records SET paper_id = ? WHERE paper_id = ?", (keep, drop)
        )
        c.execute("UPDATE hits SET paper_id = ? WHERE paper_id = ?", (keep, drop))
        c.execute(
            "UPDATE OR IGNORE scores SET paper_id = ? WHERE paper_id = ?", (keep, drop)
        )
        c.execute(
            "UPDATE OR IGNORE edges SET src_paper_id = ? WHERE src_paper_id = ?",
            (keep, drop),
        )
        c.execute(
            "UPDATE OR IGNORE edges SET dst_paper_id = ? WHERE dst_paper_id = ?",
            (keep, drop),
        )
        c.execute("DELETE FROM edges WHERE src_paper_id = dst_paper_id")
        # leftovers (rows that collided above) go with the dropped paper
        c.execute("DELETE FROM papers WHERE id = ?", (drop,))

    # ----------------------------------------------------------------- writing

    def ingest(
        self,
        papers: Sequence[PaperMeta],
        *,
        query_id: int | None,
        source: str,
        variant: str = "",
        offset: int = 0,
        round_: int = 0,
    ) -> IngestResult:
        """Store one page of results atomically and log the fetch as done."""
        n_new = 0
        with self._conn:
            for position, paper in enumerate(papers, start=1):
                gid = self.dedup.add(paper)
                paper_id = self._gid_paper.get(gid)
                if paper_id is None:
                    paper_id = self._insert_paper(self.dedup.merged(gid))
                    self._gid_paper[gid] = paper_id
                    n_new += 1
                self._upsert_record(paper_id, paper)
                self._conn.execute(
                    "INSERT INTO hits "
                    "(run_id, query_id, source, paper_id, rank, round) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.run_id,
                        query_id,
                        source,
                        paper_id,
                        offset + position,
                        round_,
                    ),
                )
                self._dirty.add(gid)
            self._flush()
            if query_id is not None:
                self._upsert_fetch(query_id, source, variant, "done", len(papers), None)
        return IngestResult(n_results=len(papers), n_new=n_new)

    def record_failed_fetch(
        self, query_id: int, source: str, variant: str, status: str, message: str
    ) -> None:
        with self._conn:
            self._upsert_fetch(query_id, source, variant, status, 0, message)

    def done_fetches(self) -> set[tuple[int, str, str]]:
        return done_fetch_keys(self._conn, self.run_id)

    def finalize_source(self, source: str, n_requests: int) -> str:
        """Summarize a source's fetches into ``source_runs``; returns its status."""
        rows = self._conn.execute(
            "SELECT status, error, n_results FROM fetches "
            "WHERE run_id = ? AND source = ? ORDER BY id",
            (self.run_id, source),
        ).fetchall()
        failed = [r for r in rows if r["status"] != "done"]
        status = "ok"
        error = None
        if failed:
            kinds = {r["status"] for r in failed}
            status = next(k for k in _FAILURE_PRECEDENCE if k in kinds)
            last = failed[-1]["error"]
            error = f"{len(failed)} of {len(rows)} fetches failed; last: {last}"
        n_results = sum(r["n_results"] for r in rows if r["status"] == "done")
        with self._conn:
            self._conn.execute(
                "INSERT INTO source_runs "
                "(run_id, source, status, n_requests, n_results, error) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, source) DO UPDATE SET status = excluded.status, "
                "n_requests = n_requests + excluded.n_requests, "
                "n_results = excluded.n_results, error = excluded.error",
                (self.run_id, source, status, n_requests, n_results, error),
            )
        return status

    # ----------------------------------------------------------------- internals

    def _insert_paper(self, paper: MergedPaper) -> int:
        marks = ", ".join("?" * 15)
        cur = self._conn.execute(
            f"INSERT INTO papers ({_PAPER_COLUMNS}) VALUES ({marks})",
            _paper_values(paper),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _flush(self) -> None:
        """Refresh the display fields of every paper whose group changed."""
        assignments = ", ".join(f"{c.strip()} = ?" for c in _PAPER_COLUMNS.split(","))
        for gid in sorted(self._dirty):
            paper_id = self._gid_paper.get(gid)
            if paper_id is not None:
                self._conn.execute(
                    f"UPDATE papers SET {assignments} WHERE id = ?",
                    (*_paper_values(self.dedup.merged(gid)), paper_id),
                )
        self._dirty.clear()

    def _upsert_record(self, paper_id: int, paper: PaperMeta) -> None:
        self._conn.execute(
            "INSERT INTO paper_records (paper_id, source, source_id, raw, meta) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source, source_id) DO UPDATE SET "
            "paper_id = excluded.paper_id, raw = excluded.raw, meta = excluded.meta",
            (
                paper_id,
                paper.source,
                paper.source_id,
                dumps_json(paper.raw),
                dumps_json(paper_to_dict(paper)),
            ),
        )

    def _upsert_fetch(
        self,
        query_id: int,
        source: str,
        variant: str,
        status: str,
        n_results: int,
        error: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO fetches "
            "(run_id, query_id, source, variant, status, n_results, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, query_id, source, variant) DO UPDATE SET "
            "status = excluded.status, n_results = excluded.n_results, "
            "error = excluded.error",
            (self.run_id, query_id, source, variant, status, n_results, error),
        )
