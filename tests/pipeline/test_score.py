from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest

from litsearch import db
from litsearch.llm import LLMError, LLMReply
from litsearch.llm.prompts import SCORE_VERSION
from litsearch.pipeline.score import (
    PaperToScore,
    build_prompt,
    context_hash,
    estimate_scoring,
    paper_input_hash,
    parse_scores,
    score_run,
)
from litsearch.pipeline.store import RunStore, add_queries
from tests.llm.fakes import FakeLLM
from tests.pipeline.fakes import FakeClock, meta


def entry(
    local_id: int, score: int = 4, query: int = 1, **extra: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": local_id,
        "score": score,
        "query": query,
        "reason": f"reason {local_id}",
        "summary": f"Summary {local_id}. Sentence two. Sentence three.",
        "tags": ["tag"],
    }
    base.update(extra)
    return base


def reply(*entries: dict[str, Any]) -> str:
    return json.dumps({"results": list(entries)})


def go(coro: Any) -> Any:
    return asyncio.run(coro)


# ------------------------------------------------------------------------ helpers


def test_context_hash_ignores_order_case_and_spacing_but_not_content() -> None:
    assert context_hash(["a b", "c d"]) == context_hash(["C  D", " a b "])
    assert context_hash(["a b"]) != context_hash(["a b", "c d"])
    assert context_hash(["a b"]) != context_hash(["a c"])


def test_input_hash_depends_on_title_abstract_and_language() -> None:
    base = PaperToScore(1, "Title", "Abstract")
    assert paper_input_hash(base, "Chinese") == paper_input_hash(
        PaperToScore(99, "Title", "Abstract"), "Chinese"
    )  # not on the database id
    assert paper_input_hash(base, "Chinese") != paper_input_hash(base, "English")
    assert paper_input_hash(base, "Chinese") != paper_input_hash(
        PaperToScore(1, "Title", "Other"), "Chinese"
    )
    assert paper_input_hash(base, "Chinese") != paper_input_hash(
        PaperToScore(1, "Other", "Abstract"), "Chinese"
    )


def test_the_prompt_numbers_queries_and_papers_and_marks_missing_abstracts() -> None:
    prompt = build_prompt(
        ["code review agents", "trust automation"],
        [
            PaperToScore(10, "First  paper", "An   abstract\nwith breaks."),
            PaperToScore(11, "Second paper", None),
            PaperToScore(12, "Third paper", "   "),
        ],
    )
    assert "1. code review agents" in prompt and "2. trust automation" in prompt
    assert "[1] Title: First paper" in prompt
    assert "Abstract: An abstract with breaks." in prompt
    assert "[2] Title: Second paper\nAbstract: (no abstract)" in prompt
    assert "[3] Title: Third paper\nAbstract: (no abstract)" in prompt
    assert "10" not in prompt  # database ids are not shown to the model


def test_very_long_abstracts_are_cut() -> None:
    prompt = build_prompt(["q"], [PaperToScore(1, "T", "word " * 2000)])
    assert len(prompt) < 2400
    assert prompt.rstrip().endswith("...")


def test_scoring_estimate() -> None:
    assert estimate_scoring(0) == (0, 0, 0)
    calls, tokens_in, tokens_out = estimate_scoring(25, batch_size=10)
    assert calls == 3
    assert tokens_in == 25 * 450 + 3 * 700
    assert tokens_out == 25 * 250


# ------------------------------------------------------------------------ parsing


def test_parse_reads_entries_by_local_id() -> None:
    parsed = parse_scores(
        reply(entry(1, 5, 2), entry(2, 0, 0)), n_queries=3, batch_size=2
    )
    assert parsed[1].score == 5 and parsed[1].query == 2
    assert parsed[1].reason == "reason 1" and parsed[1].summary.startswith("Summary 1.")
    assert parsed[1].tags == ("tag",)
    assert parsed[2].score == 0 and parsed[2].query == 0


_ONE = {"id": 1, "score": 3, "query": 1, "reason": "r", "summary": "s"}

_REPLY_SHAPES = {
    "fenced": "```json\n" + json.dumps({"results": [_ONE]}) + "\n```",
    "with prose": "Here you go: " + json.dumps({"results": [_ONE]}),
    "bare list": json.dumps([_ONE]),
    "numbers as text": json.dumps(
        {"results": [{**_ONE, "id": "1", "score": "3", "query": "1"}]}
    ),
    "whole floats": json.dumps(
        {"results": [{**_ONE, "id": 1.0, "score": 3.0, "query": 1.0}]}
    ),
}


@pytest.mark.parametrize("text", _REPLY_SHAPES.values(), ids=_REPLY_SHAPES.keys())
def test_parse_tolerates_common_reply_shapes(text: str) -> None:
    parsed = parse_scores(text, n_queries=2, batch_size=1)
    assert parsed[1].score == 3 and parsed[1].query == 1


def test_parse_drops_only_the_malformed_entries() -> None:
    entries = [
        entry(1),
        entry(2, score=6),  # outside 0..5
        entry(3, score=4.5),  # not a whole number
        entry(4, score=True),  # a boolean is not a score
        entry(9),  # id outside the batch
        entry(0),  # ids start at 1
        entry(5, summary="   "),  # nothing to show
        "not an object",
        entry(1, score=0),  # a repeated id: the first one wins
    ]
    parsed = parse_scores(reply(*entries), n_queries=2, batch_size=5)
    assert sorted(parsed) == [1]
    assert parsed[1].score == 4


def test_parse_cleans_tags_and_query_numbers() -> None:
    parsed = parse_scores(
        reply(entry(1, query=9, tags=["a", " b  c ", "", "d", "e", "f", "x" * 90])),
        n_queries=3,
        batch_size=1,
    )[1]
    assert parsed.query == 0  # no such query: treated as "none"
    assert parsed.tags == ("a", "b c", "d", "e")  # empty ones dropped, at most four
    only_long = parse_scores(reply(entry(1, tags=["x" * 90])), 1, 1)[1]
    assert len(only_long.tags[0]) == 40


@pytest.mark.parametrize(
    "text", ["", "no json", '{"other": 1}', '{"results": "x"}', "42"]
)
def test_parse_rejects_replies_without_a_result_list(text: str) -> None:
    with pytest.raises(ValueError):
        parse_scores(text, 1, 1)


# --------------------------------------------------------------------- scoring runs


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def make_run(
    conn: sqlite3.Connection, n_papers: int = 4, abstracts: bool = True
) -> tuple[int, list[int]]:
    """A run with two queries and papers whose best ranks are 1, 2, 3, ..."""
    run_id = db.create_run(conn, "h")
    (q1, _), _ = add_queries(conn, run_id, ["code review agents", "trust automation"])
    store = RunStore(conn, run_id)
    papers = [
        meta(
            f"Paper number {i} about agents",
            doi=f"10.1/{i}",
            authors=(f"Author{i} Name{i}",),
            abstract=f"Abstract {i}." if abstracts else None,
        )
        for i in range(1, n_papers + 1)
    ]
    store.ingest(papers, query_id=q1, source="openalex")
    ids = [r[0] for r in conn.execute("SELECT id FROM papers ORDER BY id")]
    return run_id, ids


def score_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM scores ORDER BY paper_id").fetchall()


def run_scoring(
    conn: sqlite3.Connection, run_id: int, llm: FakeLLM | None, **overrides: Any
) -> Any:
    options: dict[str, Any] = {
        "model": "m1",
        "language": "Chinese",
        "max_scored": 100,
    }
    options.update(overrides)
    return go(score_run(conn, llm, run_id, **options))


def test_scores_are_stored_with_their_query_basis_and_context(
    conn: sqlite3.Connection,
) -> None:
    run_id, ids = make_run(conn, 2)
    llm = FakeLLM([reply(entry(1, 5, 2, tags=["a", "b"]), entry(2, 3, 0))])
    result = run_scoring(conn, run_id, llm)
    assert (result.n_candidates, result.n_scored, result.n_failed, result.calls) == (
        2,
        2,
        0,
        1,
    )
    assert (result.input_tokens, result.output_tokens) == (100, 50)
    first, second = score_rows(conn)
    assert first["paper_id"] == ids[0] and first["score"] == 5.0
    assert first["matched_query"] == "trust automation"  # query number 2
    assert first["reason"] == "reason 1" and first["basis"] == "abstract"
    assert db.loads_json(first["tags"]) == ["a", "b"]
    assert first["prompt_version"] == SCORE_VERSION and first["model"] == "m1"
    assert first["project_hash"] == context_hash(
        ["code review agents", "trust automation"]
    )
    assert second["matched_query"] is None  # query 0 = none fits
    assert second["score"] == 2.0  # ... so the model's 3 is capped at 2


def test_the_request_carries_queries_papers_language_and_json_mode(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = make_run(conn, 2)
    llm = FakeLLM([reply(entry(1), entry(2))])
    run_scoring(conn, run_id, llm, language="English")
    call = llm.calls[0]
    assert call["model"] == "m1" and call["json_mode"] is True
    assert "in English" in call["system"] and "0 to 5" in call["system"]
    assert "addresses at least one query" in call["system"]  # relative to the queries
    assert (
        "1. code review agents" in call["user"]
        and "2. trust automation" in call["user"]
    )
    assert (
        "Paper number 1 about agents" in call["user"] and "Abstract 2." in call["user"]
    )


def test_an_answered_paper_is_never_asked_again(conn: sqlite3.Connection) -> None:
    run_id, _ = make_run(conn, 3)
    run_scoring(conn, run_id, FakeLLM([reply(entry(1), entry(2), entry(3))]))
    again = run_scoring(conn, run_id, FakeLLM([]))  # any model call would fail the test
    assert (again.n_cached, again.n_scored, again.calls) == (3, 0, 0)


@pytest.mark.parametrize(
    "change",
    [{"model": "m2"}, {"language": "English"}],
)
def test_a_change_of_model_or_language_scores_again(
    conn: sqlite3.Connection, change: dict[str, str]
) -> None:
    run_id, _ = make_run(conn, 2)
    run_scoring(conn, run_id, FakeLLM([reply(entry(1), entry(2))]))
    result = run_scoring(conn, run_id, FakeLLM([reply(entry(1), entry(2))]), **change)
    assert (result.n_cached, result.n_scored) == (0, 2)


def test_another_query_set_scores_again_because_relevance_depends_on_it(
    conn: sqlite3.Connection,
) -> None:
    run_a, _ = make_run(conn, 2)
    run_scoring(conn, run_a, FakeLLM([reply(entry(1), entry(2))]))
    run_b = db.create_run(conn, "h2")
    ((q, _),) = add_queries(conn, run_b, ["a completely different query"])
    RunStore(conn, run_b).ingest(
        [meta("Paper number 1 about agents", doi="10.1/1", authors=("Author1 Name1",))],
        query_id=q,
        source="openalex",
    )
    result = run_scoring(conn, run_b, FakeLLM([reply(entry(1))]))
    assert (result.n_cached, result.n_scored) == (0, 1)


def test_papers_are_split_into_batches(conn: sqlite3.Connection) -> None:
    run_id, _ = make_run(conn, 5)
    llm = FakeLLM(
        [reply(entry(1), entry(2)), reply(entry(1), entry(2)), reply(entry(1))]
    )
    result = run_scoring(conn, run_id, llm, batch_size=2)
    assert result.calls == 3 and result.n_scored == 5
    assert [c["user"].count("] Title:") for c in llm.calls] == [2, 2, 1]


def test_the_limit_applies_to_the_best_ranked_papers_first(
    conn: sqlite3.Connection,
) -> None:
    run_id, ids = make_run(conn, 5)
    llm = FakeLLM([reply(entry(1), entry(2))])
    result = run_scoring(conn, run_id, llm, max_scored=2)
    assert (result.n_considered, result.n_over_limit, result.n_scored) == (2, 3, 2)
    assert {r["paper_id"] for r in score_rows(conn)} == set(ids[:2])  # ranks 1 and 2


def test_cached_papers_count_towards_the_limit_but_cost_nothing(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = make_run(conn, 4)
    run_scoring(conn, run_id, FakeLLM([reply(entry(1), entry(2))]), max_scored=2)
    llm = FakeLLM([reply(entry(1), entry(2))])
    result = run_scoring(conn, run_id, llm, max_scored=4)
    assert (result.n_cached, result.n_scored, result.calls) == (2, 2, 1)
    assert llm.calls[0]["user"].count("] Title:") == 2


def test_the_call_limit_stops_scoring_and_the_rest_can_follow_later(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = make_run(conn, 4)
    first = run_scoring(
        conn,
        run_id,
        FakeLLM([reply(entry(1), entry(2))]),
        batch_size=2,
        max_llm_calls=1,
    )
    assert first.stopped == "calls" and first.calls == 1 and first.n_scored == 2
    assert first.n_failed == 0  # not attempted is not failed
    second = run_scoring(
        conn, run_id, FakeLLM([reply(entry(1), entry(2))]), batch_size=2
    )
    assert (second.n_cached, second.n_scored, second.stopped) == (2, 2, None)


def test_the_time_limit_stops_scoring(conn: sqlite3.Connection) -> None:
    run_id, _ = make_run(conn, 4)
    clock = FakeClock()
    clock.now = 100.0
    llm = FakeLLM([])
    result = run_scoring(conn, run_id, llm, deadline=50.0, clock=clock)
    assert result.stopped == "time" and llm.calls == []
    assert result.n_scored == 0


def test_missing_entries_are_asked_for_again_once(conn: sqlite3.Connection) -> None:
    run_id, ids = make_run(conn, 3)
    llm = FakeLLM([reply(entry(1), entry(3)), reply(entry(1))])  # paper 2 was left out
    result = run_scoring(conn, run_id, llm)
    assert result.calls == 2 and result.n_scored == 3 and result.n_failed == 0
    assert llm.calls[1]["user"].count("] Title:") == 1
    assert "Paper number 2" in llm.calls[1]["user"]
    assert {r["paper_id"] for r in score_rows(conn)} == set(ids)


def test_an_unreadable_reply_is_retried_and_then_given_up_on(
    conn: sqlite3.Connection,
) -> None:
    run_id, ids = make_run(conn, 2)
    llm = FakeLLM(["sorry, no json", "still no json"])
    result = run_scoring(conn, run_id, llm)
    assert result.calls == 2 and result.n_scored == 0 and result.n_failed == 2
    assert result.stopped is None  # other batches would still be tried
    assert sorted(result.failed_ids) == ids
    assert score_rows(conn) == []


def test_a_model_error_stops_scoring_but_keeps_what_was_scored(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = make_run(conn, 4)
    llm = FakeLLM([reply(entry(1), entry(2)), LLMError("rate_limited", "slow down")])
    result = run_scoring(conn, run_id, llm, batch_size=2)
    assert result.stopped == "error" and "rate_limited" in (result.error or "")
    assert result.n_scored == 2
    assert len(score_rows(conn)) == 2


def test_a_paper_without_an_abstract_is_judged_by_title_and_capped_at_four(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = make_run(conn, 1, abstracts=False)
    run_scoring(conn, run_id, FakeLLM([reply(entry(1, score=5))]))
    row = score_rows(conn)[0]
    assert row["basis"] == "title"
    assert row["score"] == 4.0


def test_a_run_without_queries_or_papers_does_nothing(conn: sqlite3.Connection) -> None:
    empty_run = db.create_run(conn, "h")
    assert run_scoring(conn, empty_run, FakeLLM([])).calls == 0
    run_id = db.create_run(conn, "h2")
    add_queries(conn, run_id, ["a query"])
    assert run_scoring(conn, run_id, FakeLLM([])).n_candidates == 0


def test_no_model_client_is_reported_when_something_needs_scoring(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = make_run(conn, 2)
    result = run_scoring(conn, run_id, None)
    assert result.stopped == "error" and "no model client" in (result.error or "")


def test_progress_is_reported_per_batch(conn: sqlite3.Connection) -> None:
    run_id, _ = make_run(conn, 3)
    messages: list[str] = []
    run_scoring(
        conn,
        run_id,
        FakeLLM([reply(entry(1), entry(2)), reply(entry(1))]),
        batch_size=2,
        progress=messages.append,
    )
    assert len(messages) == 2 and "batch 2/2" in messages[1]


def test_a_paper_that_fits_no_query_cannot_score_above_two(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = make_run(conn, 3)
    llm = FakeLLM([reply(entry(1, 5, 0), entry(2, 2, 0), entry(3, 5, 1))])
    run_scoring(conn, run_id, llm)
    scores = [r["score"] for r in score_rows(conn)]
    assert scores == [2.0, 2.0, 5.0]
    assert [r["matched_query"] for r in score_rows(conn)] == [
        None,
        None,
        "code review agents",
    ]


def test_the_prompt_tells_the_model_about_the_cap(conn: sqlite3.Connection) -> None:
    run_id, _ = make_run(conn, 1)
    llm = FakeLLM([reply(entry(1))])
    run_scoring(conn, run_id, llm)
    assert "fits no query must not score above 2" in llm.calls[0]["system"]


def test_replies_cut_off_by_the_output_limit_are_counted(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = make_run(conn, 2)
    cut = LLMReply("", "m1", 100, 6000, "length")  # a reasoning model out of tokens
    ok = LLMReply(reply(entry(1), entry(2)), "m1", 100, 50, "stop")
    result = run_scoring(conn, run_id, FakeLLM([cut, ok]))
    assert result.n_cut_off == 1
    assert result.n_scored == 2  # the second attempt succeeded
    fresh = db.connect(":memory:")
    db.init_schema(fresh)
    both_cut = run_scoring(fresh, make_run(fresh, 1)[0], FakeLLM([cut, cut]))
    assert both_cut.n_cut_off == 2 and both_cut.n_failed == 1
