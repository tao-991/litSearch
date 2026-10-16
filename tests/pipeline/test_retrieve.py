from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from typing import Any

import pytest

from litsearch import db
from litsearch.collectors.base import CollectorError, SourceStatus
from litsearch.models import Page
from litsearch.pipeline.retrieve import (
    Fetch,
    estimate,
    plan_fetches,
    run_retrieval,
)
from litsearch.pipeline.runner import (
    RunParams,
    create_run,
    execute_run,
    load_params,
    run_name,
)
from litsearch.pipeline.store import RunStore, add_queries, load_queries
from tests.pipeline.fakes import FakeClock, FakeCollector, meta


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def run_ids(conn: sqlite3.Connection, queries: tuple[str, ...]) -> tuple[int, RunStore]:
    run_id = db.create_run(conn, "h")
    add_queries(conn, run_id, queries)
    return run_id, RunStore(conn, run_id)


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def go(coro: Any) -> Any:
    return asyncio.run(coro)


# ----------------------------------------------------------------------- planning


def test_plan_has_one_fetch_per_query_and_source() -> None:
    plan = plan_fetches([(1, "a"), (2, "b")], ["openalex", "arxiv"], "title-abstract")
    assert plan["openalex"] == [
        Fetch(1, "a", "openalex", "title-abstract"),
        Fetch(2, "b", "openalex", "title-abstract"),
    ]
    assert plan["arxiv"] == [Fetch(1, "a", "arxiv"), Fetch(2, "b", "arxiv")]


def test_both_openalex_modes_double_the_openalex_fetches() -> None:
    plan = plan_fetches([(1, "a")], ["openalex", "s2"], "both")
    assert [f.variant for f in plan["openalex"]] == ["title-abstract", "fulltext"]
    assert len(plan["s2"]) == 1


def test_unknown_openalex_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        plan_fetches([(1, "a")], ["openalex"], "semantic")


def test_estimate_counts_requests_credits_and_time() -> None:
    plan = plan_fetches(
        [(1, "a"), (2, "b"), (3, "c")], ["openalex", "s2", "arxiv"], "both"
    )
    est = estimate(plan, per_query=50)
    assert est.requests == {"openalex": 6, "s2": 3, "arxiv": 3}
    assert est.total_requests == 12
    assert est.openalex_credits == 60  # 10 credits per search request
    assert est.seconds == pytest.approx(max(6 * 1.5, 3 * 2.0, 3 * 3.5))
    assert est.max_records == 6 * 50 + 3 * 50 + 3 * 50


def test_estimate_respects_the_page_limit_of_each_source() -> None:
    plan = plan_fetches([(1, "a")], ["openalex", "s2"], "title-abstract")
    assert estimate(plan, per_query=200).max_records == 200 + 100  # S2 returns <= 100


def test_estimate_skips_fetches_that_are_already_done() -> None:
    plan = plan_fetches([(1, "a"), (2, "b")], ["arxiv"], "title-abstract")
    est = estimate(plan, per_query=10, done={(1, "arxiv", "")})
    assert est.requests == {"arxiv": 1}


# --------------------------------------------------------------------- retrieval


def test_results_of_several_sources_are_stored_and_merged(
    conn: sqlite3.Connection,
) -> None:
    run_id, store = run_ids(conn, ("agents review",))
    oa_paper = meta("Agents That Review Code", arxiv_id="2501.00001", doi="10.1000/x")
    arxiv_paper = meta(
        "Agents That Review Code",
        source="arxiv",
        source_id="2501.00001",
        arxiv_id="2501.00001",
    )
    collectors = {
        "openalex": FakeCollector("openalex", {"agents review": [oa_paper]}),
        "arxiv": FakeCollector("arxiv", {"agents review": [arxiv_paper]}),
    }
    plan = plan_fetches(
        load_queries(conn, run_id), ["openalex", "arxiv"], "title-abstract"
    )
    result = go(
        run_retrieval(store, collectors, plan, per_query=15, since=date(2024, 1, 1))
    )
    assert result.statuses == {"openalex": "ok", "arxiv": "ok"}
    assert result.n_new_papers == 1  # the same paper from both sources
    assert not result.stopped_by_time
    assert count(conn, "papers") == 1
    assert count(conn, "paper_records") == 2
    assert count(conn, "hits") == 2
    assert count(conn, "source_runs") == 2
    # arguments reach the collectors: the limit, the date, and the OpenAlex mode only
    assert collectors["openalex"].calls == [
        {
            "query": "agents review",
            "limit": 15,
            "since": date(2024, 1, 1),
            "mode": "title-abstract",
        }
    ]
    assert collectors["arxiv"].calls[0]["mode"] is None


def test_a_failing_source_does_not_stop_the_others(conn: sqlite3.Connection) -> None:
    run_id, store = run_ids(conn, ("one", "two"))
    boom = CollectorError(SourceStatus.RATE_LIMITED, "s2: HTTP 429 after retries")
    collectors = {
        "openalex": FakeCollector(
            "openalex",
            {
                "one": [meta("Paper one on agents", doi="10.1/a")],
                "two": [meta("Paper two on review", doi="10.1/b", authors=("X Y",))],
            },
        ),
        "s2": FakeCollector("s2", {"one": boom, "two": boom}),
    }
    plan = plan_fetches(
        load_queries(conn, run_id), ["openalex", "s2"], "title-abstract"
    )
    result = go(run_retrieval(store, collectors, plan, per_query=10))
    assert result.statuses == {"openalex": "ok", "s2": "rate_limited"}
    assert count(conn, "papers") == 2
    failed = conn.execute(
        "SELECT status, error FROM fetches WHERE source = 's2'"
    ).fetchall()
    assert [r["status"] for r in failed] == ["rate_limited", "rate_limited"]
    row = conn.execute("SELECT * FROM source_runs WHERE source = 's2'").fetchone()
    assert row["status"] == "rate_limited"
    assert row["n_results"] == 0
    assert row["n_requests"] == 2


def test_resume_repeats_only_the_fetches_that_have_not_finished(
    conn: sqlite3.Connection,
) -> None:
    run_id, store = run_ids(conn, ("one", "two"))
    paper_one = meta("Paper one on agents", doi="10.1/a")
    paper_two = meta("Paper two on review", doi="10.1/b", authors=("X Y",))
    flaky = FakeCollector(
        "s2",
        {"one": [paper_one], "two": CollectorError(SourceStatus.RATE_LIMITED, "429")},
    )
    plan = plan_fetches(load_queries(conn, run_id), ["s2"], "title-abstract")
    first = go(run_retrieval(store, {"s2": flaky}, plan, per_query=10))
    assert first.statuses == {"s2": "rate_limited"}

    flaky.script["two"] = [paper_two]
    flaky.calls.clear()
    resumed_store = RunStore(conn, run_id)  # as after a restart
    second = go(run_retrieval(resumed_store, {"s2": flaky}, plan, per_query=10))
    assert [c["query"] for c in flaky.calls] == ["two"]  # "one" was not fetched again
    assert second.statuses == {"s2": "ok"}
    assert count(conn, "papers") == 2
    row = conn.execute("SELECT * FROM source_runs").fetchone()
    assert (row["status"], row["n_requests"], row["n_results"]) == ("ok", 3, 2)


def test_the_time_limit_stops_the_run_and_a_resume_finishes_it(
    conn: sqlite3.Connection,
) -> None:
    run_id, store = run_ids(conn, ("one", "two", "three"))
    clock = FakeClock()
    script = {
        q: [meta(f"Paper about {q} agents", doi=f"10.1/{q}", authors=(f"{q} Person",))]
        for q in ("one", "two", "three")
    }
    collector = FakeCollector("arxiv", script, clock=clock, seconds_per_call=40)
    plan = plan_fetches(load_queries(conn, run_id), ["arxiv"], "title-abstract")
    first = go(
        run_retrieval(
            store, {"arxiv": collector}, plan, per_query=10, deadline=60, clock=clock
        )
    )
    assert first.stopped_by_time
    assert first.pending == 1  # two fetches ran (40 s and 80 s), one is left
    assert count(conn, "papers") == 2

    collector.calls.clear()
    second = go(
        run_retrieval(RunStore(conn, run_id), {"arxiv": collector}, plan, per_query=10)
    )
    assert [c["query"] for c in collector.calls] == ["three"]
    assert not second.stopped_by_time
    assert count(conn, "papers") == 3


def test_a_query_a_source_cannot_express_is_not_a_failure(
    conn: sqlite3.Connection,
) -> None:
    run_id, store = run_ids(conn, ("the of",))

    class Picky(FakeCollector):
        async def search(self, query: str, **kwargs: Any) -> Page:
            self.n_requests += 1
            raise ValueError("query has no searchable words")

    plan = plan_fetches(load_queries(conn, run_id), ["arxiv"], "title-abstract")
    result = go(run_retrieval(store, {"arxiv": Picky("arxiv", {})}, plan, per_query=10))
    assert result.statuses == {"arxiv": "ok"}
    assert conn.execute("SELECT status FROM fetches").fetchone()[0] == "done"


@pytest.mark.parametrize("per_query", [0, 201])
def test_per_query_outside_the_supported_range_is_rejected(
    conn: sqlite3.Connection, per_query: int
) -> None:
    _, store = run_ids(conn, ("one",))
    with pytest.raises(ValueError):
        go(run_retrieval(store, {}, {}, per_query=per_query))


def test_progress_messages_are_reported(conn: sqlite3.Connection) -> None:
    run_id, store = run_ids(conn, ("agents review",))
    collectors = {
        "arxiv": FakeCollector(
            "arxiv", {"agents review": [meta("A paper on agents", doi="10.1/a")]}
        )
    }
    plan = plan_fetches(load_queries(conn, run_id), ["arxiv"], "title-abstract")
    messages: list[str] = []
    go(run_retrieval(store, collectors, plan, per_query=10, progress=messages.append))
    assert len(messages) == 1
    assert "1 papers (1 new)" in messages[0]


# ------------------------------------------------------------------------- runner


def params(**overrides: Any) -> RunParams:
    base: dict[str, Any] = {
        "queries": ("agents review", "trust"),
        "sources": ("openalex", "arxiv"),
        "per_query": 20,
        "openalex_mode": "title-abstract",
        "profile": "quick",
    }
    base.update(overrides)
    return RunParams(**base)


def test_create_run_stores_name_queries_and_parameters(
    conn: sqlite3.Connection,
) -> None:
    run_id = create_run(conn, params(since="2024-01-01", max_minutes=5.0))
    assert run_name(conn, run_id) == f"run-{run_id:04d}"
    assert [t for _, t in load_queries(conn, run_id)] == ["agents review", "trust"]
    assert load_params(conn, run_id) == params(since="2024-01-01", max_minutes=5.0)
    named = create_run(conn, params(), name="my-search")
    assert run_name(conn, named) == "my-search"


def test_loading_a_missing_run_is_an_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        load_params(conn, 99)


def test_execute_run_completes_and_records_status(conn: sqlite3.Connection) -> None:
    run_id = create_run(conn, params())
    collectors = {
        "openalex": FakeCollector(
            "openalex", {"agents review": [meta("Agents review paper", doi="10.1/a")]}
        ),
        "arxiv": FakeCollector("arxiv", {}),
    }
    result = go(execute_run(conn, run_id, collectors))
    assert result.statuses == {"openalex": "ok", "arxiv": "ok"}
    run = db.get_run(conn, run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["finished_at"] is not None


def test_execute_run_is_interrupted_by_the_time_limit(conn: sqlite3.Connection) -> None:
    run_id = create_run(conn, params(sources=("arxiv",)))
    clock = FakeClock()
    collector = FakeCollector("arxiv", {}, clock=clock, seconds_per_call=120)
    result = go(
        execute_run(conn, run_id, {"arxiv": collector}, max_minutes=1, clock=clock)
    )
    assert result.stopped_by_time
    run = db.get_run(conn, run_id)
    assert run is not None
    assert run["status"] == "interrupted"


def test_a_crash_marks_the_run_failed_and_propagates(conn: sqlite3.Connection) -> None:
    run_id = create_run(conn, params(sources=("arxiv",)))

    class Broken(FakeCollector):
        async def search(self, query: str, **kwargs: Any) -> Page:
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        go(execute_run(conn, run_id, {"arxiv": Broken("arxiv", {})}))
    run = db.get_run(conn, run_id)
    assert run is not None
    assert run["status"] == "failed"


def test_cancellation_marks_the_run_interrupted(conn: sqlite3.Connection) -> None:
    run_id = create_run(conn, params(sources=("arxiv",)))

    class Slow(FakeCollector):
        async def search(self, query: str, **kwargs: Any) -> Page:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        go(execute_run(conn, run_id, {"arxiv": Slow("arxiv", {})}))
    run = db.get_run(conn, run_id)
    assert run is not None
    assert run["status"] == "interrupted"
