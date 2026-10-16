"""L1 screening (SPEC §3.5): the model reads title and abstract and scores each paper.

A paper is scored against the run's *queries* (the sub-questions of the review), not
against the whole project: it counts as relevant if it addresses at least one query.
Scores go from 0 to 5. The model only returns scores, reasons, summaries and tags;
links, DOIs, authors and years always come from the data sources.

Scores are cached in ``scores``, keyed by (paper, query set, input hash, prompt version,
model), so rescoring a run, or scoring a paper found again by another run with the same
queries, costs nothing.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from litsearch.db import dumps_json
from litsearch.llm import LLMClient, LLMError
from litsearch.llm.jsonutil import extract_json
from litsearch.llm.prompts import SCORE_SYSTEM, SCORE_VERSION
from litsearch.pipeline.store import load_queries

MAX_SCORE = 5
DEFAULT_BATCH_SIZE = 10
MAX_ABSTRACT_CHARS = 2000
MAX_TAGS = 4
# Rough sizes for estimates only.
EST_INPUT_TOKENS_PER_PAPER = 450
EST_OUTPUT_TOKENS_PER_PAPER = 250
EST_INPUT_TOKENS_PER_CALL = 700


@dataclass(frozen=True, slots=True)
class PaperToScore:
    paper_id: int
    title: str
    abstract: str | None

    @property
    def basis(self) -> str:
        return "abstract" if (self.abstract or "").strip() else "title"


@dataclass(frozen=True, slots=True)
class ParsedScore:
    score: int
    query: int  # 1-based number of the best matching query, 0 = none
    reason: str
    summary: str
    tags: tuple[str, ...]


@dataclass(slots=True)
class ScoreResult:
    n_candidates: int = 0  # papers the run found
    n_considered: int = 0  # the first ``max_scored`` of them, best ranked first
    n_cached: int = 0  # already scored: no model call needed
    n_scored: int = 0  # scored by the model in this call
    n_failed: int = 0  # the model did not return a usable score
    n_over_limit: int = 0  # found but beyond ``max_scored``
    calls: int = 0
    n_cut_off: int = 0  # replies cut off by the output limit (reasoning models do this)
    input_tokens: int = 0
    output_tokens: int = 0
    stopped: str | None = None  # "calls", "time" or "error"
    error: str | None = None
    failed_ids: list[int] = field(default_factory=list)


def context_hash(queries: Sequence[str]) -> str:
    """Identity of the query set a score is relative to (order does not matter)."""
    basis = "\n".join(sorted(" ".join(q.split()).lower() for q in queries))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def paper_input_hash(paper: PaperToScore, language: str) -> str:
    digest = hashlib.sha256()
    for part in (paper.title, paper.abstract or "", language):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def build_prompt(queries: Sequence[str], batch: Sequence[PaperToScore]) -> str:
    lines = ["Queries:"]
    lines += [f"{i}. {q}" for i, q in enumerate(queries, start=1)]
    lines += ["", "Papers:"]
    for local_id, paper in enumerate(batch, start=1):
        lines.append(f"[{local_id}] Title: {' '.join(paper.title.split())}")
        abstract = " ".join((paper.abstract or "").split())
        if abstract:
            if len(abstract) > MAX_ABSTRACT_CHARS:
                abstract = abstract[:MAX_ABSTRACT_CHARS].rstrip() + " ..."
            lines.append(f"Abstract: {abstract}")
        else:
            lines.append("Abstract: (no abstract)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_scores(text: str, n_queries: int, batch_size: int) -> dict[int, ParsedScore]:
    """Valid entries of a model reply by local paper id (1..batch_size).

    Entries that are malformed (bad id, score outside 0..5, no text) are left out, so
    the caller can ask again for just those papers. Raises ``ValueError`` if the reply
    is not the expected JSON at all.
    """
    data = extract_json(text)
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("the reply has no list of results")
    found: dict[int, ParsedScore] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        local_id = _int(item.get("id"))
        score = _int(item.get("score"))
        if local_id is None or not 1 <= local_id <= batch_size or local_id in found:
            continue
        if score is None or not 0 <= score <= MAX_SCORE:
            continue
        reason = " ".join(str(item.get("reason") or "").split())
        summary = " ".join(str(item.get("summary") or "").split())
        if not summary:
            continue
        query = _int(item.get("query")) or 0
        raw_tags = item.get("tags")
        tags = tuple(
            t
            for tag in (raw_tags if isinstance(raw_tags, list) else [])
            if (t := " ".join(str(tag).split())[:40])
        )[:MAX_TAGS]
        found[local_id] = ParsedScore(
            score=score,
            query=query if 0 <= query <= n_queries else 0,
            reason=reason,
            summary=summary,
            tags=tags,
        )
    return found


def estimate_scoring(
    n_papers: int, batch_size: int = DEFAULT_BATCH_SIZE
) -> tuple[int, int, int]:
    """(model calls, input tokens, output tokens), roughly, for ``n_papers`` papers."""
    if n_papers <= 0:
        return 0, 0, 0
    calls = math.ceil(n_papers / batch_size)
    return (
        calls,
        n_papers * EST_INPUT_TOKENS_PER_PAPER + calls * EST_INPUT_TOKENS_PER_CALL,
        n_papers * EST_OUTPUT_TOKENS_PER_PAPER,
    )


def _candidates(conn: sqlite3.Connection, run_id: int) -> list[PaperToScore]:
    """The run's papers, best ranked first (best rank in any source list, then by how
    many queries found them)."""
    rows = conn.execute(
        "SELECT p.id, p.title, p.abstract, MIN(h.rank) AS best_rank, "
        "COUNT(DISTINCT h.query_id) AS n_queries "
        "FROM papers p JOIN hits h ON h.paper_id = p.id WHERE h.run_id = ? "
        "GROUP BY p.id ORDER BY best_rank IS NULL, best_rank, n_queries DESC, p.id",
        (run_id,),
    ).fetchall()
    return [PaperToScore(r["id"], r["title"], r["abstract"]) for r in rows]


def _is_cached(
    conn: sqlite3.Connection, paper: PaperToScore, ctx: str, language: str, model: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM scores WHERE paper_id = ? AND project_hash = ? "
        "AND input_hash = ? AND prompt_version = ? AND model = ?",
        (paper.paper_id, ctx, paper_input_hash(paper, language), SCORE_VERSION, model),
    ).fetchone()
    return row is not None


def _store(
    conn: sqlite3.Connection,
    paper: PaperToScore,
    parsed: ParsedScore,
    queries: Sequence[str],
    ctx: str,
    language: str,
    model: str,
) -> None:
    score = parsed.score
    if paper.basis == "title":
        score = min(score, 4)  # a title alone is not enough for the top score
    if parsed.query < 1:
        score = min(score, 2)  # a paper that fits no query is not relevant
    matched = queries[parsed.query - 1] if parsed.query >= 1 else None
    with conn:
        conn.execute(
            "INSERT INTO scores (paper_id, project_hash, input_hash, prompt_version, "
            "model, score, reason, summary, tags, matched_query, basis) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(paper_id, project_hash, input_hash, prompt_version, model) "
            "DO UPDATE SET score = excluded.score, reason = excluded.reason, "
            "summary = excluded.summary, tags = excluded.tags, "
            "matched_query = excluded.matched_query, basis = excluded.basis",
            (
                paper.paper_id,
                ctx,
                paper_input_hash(paper, language),
                SCORE_VERSION,
                model,
                float(score),
                parsed.reason,
                parsed.summary,
                dumps_json(list(parsed.tags)),
                matched,
                paper.basis,
            ),
        )


@dataclass(frozen=True, slots=True)
class ScoringPlan:
    n_candidates: int
    n_considered: int  # the first ``max_scored`` papers, best ranked first
    n_cached: int
    n_to_score: int  # papers that still need a model call


def plan_scoring(
    conn: sqlite3.Connection, run_id: int, *, model: str, language: str, max_scored: int
) -> ScoringPlan:
    """How much scoring a run still needs, without calling the model."""
    queries = [text for _, text in load_queries(conn, run_id)]
    everyone = _candidates(conn, run_id)
    considered = everyone[:max_scored]
    if not queries:
        return ScoringPlan(len(everyone), len(considered), 0, 0)
    ctx = context_hash(queries)
    cached = sum(1 for p in considered if _is_cached(conn, p, ctx, language, model))
    return ScoringPlan(len(everyone), len(considered), cached, len(considered) - cached)


async def score_run(
    conn: sqlite3.Connection,
    llm: LLMClient | None,
    run_id: int,
    *,
    model: str,
    language: str,
    max_scored: int,
    max_llm_calls: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[str], None] | None = None,
) -> ScoreResult:
    """Score the papers of a run that have no score yet (best ranked first)."""
    say = progress or (lambda _msg: None)
    queries = [text for _, text in load_queries(conn, run_id)]
    result = ScoreResult()
    if not queries:
        return result
    ctx = context_hash(queries)
    everyone = _candidates(conn, run_id)
    considered = everyone[:max_scored]
    result.n_candidates = len(everyone)
    result.n_considered = len(considered)
    result.n_over_limit = len(everyone) - len(considered)
    todo = [p for p in considered if not _is_cached(conn, p, ctx, language, model)]
    result.n_cached = len(considered) - len(todo)
    if not todo:
        return result
    if llm is None:
        result.stopped, result.error = "error", "no model client available"
        return result

    system = SCORE_SYSTEM.format(language=language)
    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    for number, batch in enumerate(batches, start=1):
        pending = list(batch)
        for _ in range(2):  # the second attempt asks again for the missing ones
            if max_llm_calls is not None and result.calls >= max_llm_calls:
                result.stopped = "calls"
                break
            if deadline is not None and clock() >= deadline:
                result.stopped = "time"
                break
            prompt = build_prompt(queries, pending)
            try:
                reply = await llm.complete(
                    model=model,
                    system=system,
                    user=prompt,
                    max_tokens=min(8000, 450 * len(pending) + 1500),
                    json_mode=True,
                )
            except LLMError as exc:
                result.stopped, result.error = "error", f"{exc.kind}: {exc.message}"
                break
            result.calls += 1
            if reply.finish_reason in ("length", "max_tokens"):
                result.n_cut_off += 1
            result.input_tokens += reply.input_tokens or 0
            result.output_tokens += reply.output_tokens or 0
            try:
                parsed = parse_scores(reply.text, len(queries), len(pending))
            except ValueError:
                parsed = {}
            still: list[PaperToScore] = []
            for local_id, paper in enumerate(pending, start=1):
                if local_id in parsed:
                    _store(conn, paper, parsed[local_id], queries, ctx, language, model)
                    result.n_scored += 1
                else:
                    still.append(paper)
            pending = still
            if not pending:
                break
        if pending and result.stopped is None:
            result.n_failed += len(pending)
            result.failed_ids += [p.paper_id for p in pending]
        say(
            f"scoring batch {number}/{len(batches)}: {result.n_scored} scored so far, "
            f"{result.calls} model calls"
        )
        if result.stopped:
            break
    return result
