"""Retrieval (SPEC §3.3): fetch results of every query from every source and store them.

Sources run side by side (``asyncio.gather``) but each source is worked through one
request at a time. Every fetch is committed together with its results, so a stopped run
can be resumed without repeating finished fetches. A failing source never fails the run:
the failure is logged for ``source_runs`` and the other sources carry on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from litsearch.collectors.base import CollectorError
from litsearch.collectors.openalex import SEARCH_MODES, estimate_credits
from litsearch.pipeline.store import RunStore

logger = logging.getLogger(__name__)

OPENALEX_MODES = (*SEARCH_MODES, "both")
MAX_PER_QUERY = 200  # one request returns at most this many (OpenAlex, arXiv)

# Typical seconds per request including the politeness gap (used only for estimates).
REQUEST_SECONDS = {"openalex": 1.5, "s2": 2.0, "arxiv": 3.5}
# Most results one request can return, per source.
PAGE_LIMIT = {"openalex": 200, "s2": 100, "arxiv": 200}


@dataclass(frozen=True, slots=True)
class Fetch:
    """One request to make: a query against a source (and OpenAlex search mode)."""

    query_id: int
    query: str
    source: str
    variant: str = ""

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.query_id, self.source, self.variant)


def plan_fetches(
    queries: Sequence[tuple[int, str]],
    sources: Sequence[str],
    openalex_mode: str,
) -> dict[str, list[Fetch]]:
    """The ordered fetches of every source."""
    if openalex_mode not in OPENALEX_MODES:
        raise ValueError(f"openalex mode must be one of {OPENALEX_MODES}")
    modes = SEARCH_MODES if openalex_mode == "both" else (openalex_mode,)
    plan: dict[str, list[Fetch]] = {}
    for source in sources:
        if source == "openalex":
            plan[source] = [
                Fetch(qid, text, source, m) for qid, text in queries for m in modes
            ]
        else:
            plan[source] = [Fetch(qid, text, source) for qid, text in queries]
    return plan


@dataclass(frozen=True, slots=True)
class Estimate:
    """What a run is expected to cost (an upper bound on results, a rough duration)."""

    requests: dict[str, int]
    openalex_credits: int
    seconds: float  # sources run side by side, so it is the slowest source
    max_records: int

    @property
    def total_requests(self) -> int:
        return sum(self.requests.values())


def estimate(
    plan: Mapping[str, Sequence[Fetch]],
    per_query: int,
    done: set[tuple[int, str, str]] | None = None,
) -> Estimate:
    """Estimate the fetches of ``plan`` that are not already ``done``."""
    done = done or set()
    requests = {
        source: sum(1 for f in fetches if f.key not in done)
        for source, fetches in plan.items()
    }
    seconds = max(
        (n * REQUEST_SECONDS.get(s, 3.0) for s, n in requests.items()), default=0.0
    )
    records = sum(
        n * min(per_query, PAGE_LIMIT.get(s, per_query)) for s, n in requests.items()
    )
    return Estimate(
        requests=requests,
        openalex_credits=estimate_credits(requests.get("openalex", 0)),
        seconds=seconds,
        max_records=records,
    )


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    statuses: dict[str, str]  # source -> ok / blocked / rate_limited / error / skipped
    n_new_papers: int
    pending: int  # fetches not attempted because the time limit was reached
    stopped_by_time: bool


async def run_retrieval(
    store: RunStore,
    collectors: Mapping[str, Any],
    plan: Mapping[str, Sequence[Fetch]],
    *,
    per_query: int,
    since: date | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[str], None] | None = None,
) -> RetrievalResult:
    """Work through ``plan``; fetches already done in this run are skipped."""
    if not 1 <= per_query <= MAX_PER_QUERY:
        raise ValueError(f"per_query must be between 1 and {MAX_PER_QUERY}")
    done = store.done_fetches()
    say = progress or (lambda _msg: None)
    n_new_total = 0
    pending_total = 0
    stopped = False

    async def work(source: str, fetches: Sequence[Fetch]) -> None:
        nonlocal n_new_total, pending_total, stopped
        collector = collectors[source]
        requests_before = getattr(collector, "n_requests", 0)
        todo = [f for f in fetches if f.key not in done]
        for index, fetch in enumerate(todo, start=1):
            if deadline is not None and clock() >= deadline:
                stopped = True
                pending_total += len(todo) - index + 1
                left = len(todo) - index + 1
                say(f"{source:9} time limit reached; {left} fetches left")
                break
            label = f"{source:9} [{index}/{len(todo)}] {fetch.query[:50]!r}"
            extra = (
                {"mode": fetch.variant}
                if source == "openalex" and fetch.variant
                else {}
            )
            try:
                page = await collector.search(
                    fetch.query, limit=per_query, since=since, **extra
                )
            except CollectorError as exc:
                store.record_failed_fetch(
                    fetch.query_id, source, fetch.variant, exc.status.value, exc.message
                )
                say(f"{label} -> FAILED ({exc.status.value})")
                continue
            except ValueError as exc:
                # this query cannot be expressed for this source: not a source failure
                store.ingest(
                    [], query_id=fetch.query_id, source=source, variant=fetch.variant
                )
                logger.info("%s: query %r not usable: %s", source, fetch.query, exc)
                say(f"{label} -> query not usable for this source")
                continue
            result = store.ingest(
                page.items,
                query_id=fetch.query_id,
                source=source,
                variant=fetch.variant,
            )
            n_new_total += result.n_new
            say(f"{label} -> {result.n_results} papers ({result.n_new} new)")
        requests = getattr(collector, "n_requests", 0) - requests_before
        statuses[source] = store.finalize_source(source, requests)

    statuses: dict[str, str] = {}
    await asyncio.gather(*(work(s, f) for s, f in plan.items()))
    return RetrievalResult(
        statuses=statuses,
        n_new_papers=n_new_total,
        pending=pending_total,
        stopped_by_time=stopped,
    )
