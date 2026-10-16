"""Creating, resuming and executing a retrieval run (what ``litsearch run`` does)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from litsearch import db
from litsearch.pipeline.retrieve import (
    RetrievalResult,
    plan_fetches,
    run_retrieval,
)
from litsearch.pipeline.store import RunStore, add_queries, load_queries


@dataclass(frozen=True, slots=True)
class RunParams:
    """Everything that defines a run; stored in ``runs.params`` so it can be resumed."""

    queries: tuple[str, ...]
    sources: tuple[str, ...]
    per_query: int
    openalex_mode: str
    profile: str
    since: str | None = None  # ISO date
    max_minutes: float | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunParams:
        fields = dict(data)
        fields["queries"] = tuple(fields["queries"])
        fields["sources"] = tuple(fields["sources"])
        return cls(**fields)


def create_run(
    conn: sqlite3.Connection, params: RunParams, name: str | None = None
) -> int:
    """Store a new run with its queries; the default name is ``run-0001`` style."""
    project_hash = hashlib.sha256("\n".join(params.queries).encode()).hexdigest()
    run_id = db.create_run(conn, project_hash, params.to_json())
    with conn:
        conn.execute(
            "UPDATE runs SET name = ? WHERE id = ?",
            (name or f"run-{run_id:04d}", run_id),
        )
    add_queries(conn, run_id, params.queries, origin="user")
    return run_id


def load_params(conn: sqlite3.Connection, run_id: int) -> RunParams:
    row = db.get_run(conn, run_id)
    if row is None:
        raise LookupError(f"no run with id {run_id}")
    return RunParams.from_json(json.loads(row["params"]))


def run_name(conn: sqlite3.Connection, run_id: int) -> str:
    row = db.get_run(conn, run_id)
    if row is None:
        raise LookupError(f"no run with id {run_id}")
    return str(row["name"] or f"run-{run_id:04d}")


async def execute_run(
    conn: sqlite3.Connection,
    run_id: int,
    collectors: Mapping[str, Any],
    *,
    max_minutes: float | None = None,
    progress: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RetrievalResult:
    """Fetch everything the run still needs; leaves the run completed or interrupted.

    ``max_minutes`` overrides the value stored with the run (useful when resuming).
    """
    params = load_params(conn, run_id)
    store = RunStore(conn, run_id)
    plan = plan_fetches(
        load_queries(conn, run_id), params.sources, params.openalex_mode
    )
    limit = max_minutes if max_minutes is not None else params.max_minutes
    deadline = clock() + limit * 60 if limit else None
    since = date.fromisoformat(params.since) if params.since else None

    db.set_run_status(conn, run_id, "running")
    try:
        result = await run_retrieval(
            store,
            collectors,
            plan,
            per_query=params.per_query,
            since=since,
            deadline=deadline,
            clock=clock,
            progress=progress,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        db.set_run_status(conn, run_id, "interrupted")
        raise
    except Exception:
        db.set_run_status(conn, run_id, "failed")
        raise
    db.set_run_status(conn, run_id, "interrupted" if result.pending else "completed")
    return result
