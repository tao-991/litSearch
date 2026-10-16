"""Writing the result files of a run (HTML, Markdown, CSV) from what is stored."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from litsearch.pipeline import export
from litsearch.pipeline.html_report import write_html
from litsearch.pipeline.runner import run_name


def write_outputs(
    conn: sqlite3.Connection,
    run_id: int,
    out_dir: Path,
    *,
    top: int = 50,
    min_score: int = export.DEFAULT_MIN_SCORE,
    maybe_score: int = export.DEFAULT_MAYBE_SCORE,
    prefer_model: str | None = None,
) -> tuple[export.RunSummary, Path]:
    """Write ``candidates.html``, ``.md`` and ``.csv`` into ``out_dir/<run name>/``."""
    folder = out_dir / run_name(conn, run_id)
    candidates = export.load_candidates(conn, run_id, prefer_model)
    summary = export.load_summary(conn, run_id, prefer_model)
    export.write_csv(candidates, folder / "candidates.csv")
    export.write_markdown(
        candidates,
        summary,
        folder / "candidates.md",
        top=top,
        min_score=min_score,
        maybe_score=maybe_score,
    )
    write_html(
        candidates,
        summary,
        folder / "candidates.html",
        min_score=min_score,
        maybe_score=maybe_score,
    )
    return summary, folder
