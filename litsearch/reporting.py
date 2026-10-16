"""Texts and options shared by the command line and the web interface.

Pure functions: they build the plan and result descriptions and validate the scoring
options, and know nothing about Typer or HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass

from litsearch.config import Settings
from litsearch.pipeline import export
from litsearch.pipeline.retrieve import Estimate, RetrievalResult
from litsearch.pipeline.runner import RunParams
from litsearch.pipeline.score import ScoreResult, estimate_scoring
from litsearch.profiles import PROFILES


def queries_phrase(count: int) -> str:
    return f"{count} {'query' if count == 1 else 'queries'}"


def duration(seconds: float) -> str:
    if seconds < 90:
        return f"about {max(1, round(seconds))} s"
    return f"about {round(seconds / 60)} min"


def plan_lines(
    params: RunParams,
    est: Estimate,
    quota: tuple[int | None, int | None] | None,
    *,
    resuming: bool,
) -> list[str]:
    modes = f" ({params.openalex_mode})" if "openalex" in params.sources else ""
    requests = ", ".join(f"{s} {n}" for s, n in est.requests.items())
    limit = (
        "no time limit"
        if not params.max_minutes
        else f"time limit {params.max_minutes:g} min"
    )
    lines = [
        f"{'Remaining work of the run' if resuming else 'Plan'}: "
        f"{queries_phrase(len(params.queries))}, "
        f"sources {', '.join(params.sources)}{modes}, "
        f"up to {params.per_query} papers per query and source, "
        f"profile {params.profile}, {limit}",
        f"Requests: {requests} ({est.total_requests} in total)",
        f"Time: {duration(est.seconds)} (sources run side by side)",
        f"At most {est.max_records} results before duplicates are merged",
    ]
    if "openalex" in params.sources:
        credits = f"OpenAlex credits needed: about {est.openalex_credits}"
        if quota and quota[0] is not None:
            credits += f" (remaining today: {quota[0]} of {quota[1]})"
        lines.append(credits)
        if quota and quota[0] is not None and quota[0] < est.openalex_credits:
            lines.append(
                "Warning: this may use more OpenAlex credits than remain today; "
                "OpenAlex requests will fail once they run out (other sources are "
                "not affected)."
            )
    return lines


def result_lines(summary: export.RunSummary, result: RetrievalResult) -> list[str]:
    if result.stopped_by_time:
        head = (
            f"Run {summary.name} stopped at the time limit "
            f"({result.pending} requests left). Continue with: "
            f"litsearch run --resume {summary.run_id}"
        )
    else:
        head = f"Run {summary.name} {summary.status}."
    lines = [head, "Sources:"]
    for s in summary.sources:
        note = f"  ({s.error})" if s.error else ""
        lines.append(
            f"  {s.source:9} {s.status:13} {s.n_requests:3} requests "
            f"{s.n_results:4} results{note}"
        )
    lines.append(
        f"Papers: {summary.n_papers} distinct, "
        f"{result.n_new_papers} new to the database"
    )
    return lines


@dataclass(frozen=True, slots=True)
class ScoringOptions:
    """How (and whether) papers are scored after the search."""

    enabled: bool
    off_reason: str | None
    model: str
    max_scored: int
    max_llm_calls: int | None
    batch_size: int
    min_score: int
    maybe_score: int


def scoring_options(
    settings: Settings,
    profile: str,
    *,
    no_score: bool,
    max_scored: int | None,
    max_llm_calls: int | None,
    batch_size: int,
    min_score: int,
    maybe_score: int,
) -> ScoringOptions:
    if not 1 <= batch_size <= 30:
        raise ValueError("--score-batch must be between 1 and 30")
    if not 0 <= maybe_score <= min_score <= 5:
        raise ValueError(
            "--min-score and --maybe-score must satisfy 0 <= maybe <= min <= 5"
        )
    if max_scored is not None and max_scored < 1:
        raise ValueError("--max-scored must be at least 1")
    if max_llm_calls is not None and max_llm_calls < 0:
        raise ValueError("--max-llm-calls must not be negative")
    reason = None
    if no_score:
        reason = "--no-score"
    elif not settings.llm_api_key or not settings.l1_model:
        reason = "LLM_API_KEY or L1_MODEL is empty in .env"
    chosen = max_scored if max_scored is not None else PROFILES[profile].max_scored
    return ScoringOptions(
        enabled=reason is None,
        off_reason=reason,
        model=settings.l1_model or "",
        max_scored=chosen,
        max_llm_calls=max_llm_calls,
        batch_size=batch_size,
        min_score=min_score,
        maybe_score=maybe_score,
    )


def scoring_plan_lines(
    opts: ScoringOptions, n_upper_bound: int, already: tuple[int, int] | None
) -> list[str]:
    """Plan lines for scoring; ``already`` = (to score, cached) for an existing run."""
    if not opts.enabled:
        return [
            f"Scoring: skipped ({opts.off_reason}); the list will not be ranked by "
            "relevance"
        ]
    n = already[0] if already else min(opts.max_scored, n_upper_bound)
    calls, tokens_in, tokens_out = estimate_scoring(n, opts.batch_size)
    if opts.max_llm_calls is not None:
        calls = min(calls, opts.max_llm_calls)
    cached = f", {already[1]} already scored" if already and already[1] else ""
    return [
        f"Scoring: up to {n} papers (best ranked first{cached}) in about {calls} model "
        f"calls with {opts.model}; roughly {tokens_in} tokens in / {tokens_out} out",
        "          (what that costs depends on your API; the model only reads titles "
        "and abstracts)",
    ]


def score_lines(result: ScoreResult, opts: ScoringOptions, run_id: int) -> list[str]:
    lines = [
        f"Scoring: {result.n_scored} papers scored by the model, {result.n_cached} "
        f"already scored before, {result.calls} model calls, "
        f"{result.input_tokens} tokens in / {result.output_tokens} out"
    ]
    if result.n_over_limit:
        lines.append(
            f"  {result.n_over_limit} more papers were found than the limit of "
            f"{opts.max_scored} allows; they are not scored (see --max-scored)."
        )
    if result.n_failed:
        lines.append(
            f"  {result.n_failed} papers got no usable score from the model; try "
            f"again with: litsearch score {run_id}"
        )
    if result.n_cut_off:
        lines.append(
            f"  {result.n_cut_off} of {result.calls} model replies were cut off by the "
            "output limit. A model that spends its output on hidden reasoning does "
            "this; use a model without reasoning as L1_MODEL (see check-llm)."
        )
    if result.stopped == "calls":
        lines.append(f"  Stopped at the limit of {opts.max_llm_calls} model calls.")
    elif result.stopped == "time":
        lines.append("  Stopped at the time limit.")
    elif result.stopped == "error":
        lines.append(f"  Stopped because of a model error ({result.error}).")
    if result.stopped:
        lines.append(f"  Continue with: litsearch score {run_id}")
    return lines
