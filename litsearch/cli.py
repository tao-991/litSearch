"""Command-line entry point (SPEC §9). Commands are added as the pipeline is built."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import typer

from litsearch import __version__, db
from litsearch.collectors import SOURCE_NAMES, build_client, build_collectors
from litsearch.collectors.base import CollectorError
from litsearch.config import Settings
from litsearch.llm import LLMError, build_llm
from litsearch.pipeline import export
from litsearch.pipeline.retrieve import (
    MAX_PER_QUERY,
    OPENALEX_MODES,
    Estimate,
    RetrievalResult,
    estimate,
    plan_fetches,
)
from litsearch.pipeline.runner import (
    RunParams,
    create_run,
    execute_run,
    load_params,
    run_name,
)
from litsearch.pipeline.store import done_fetch_keys, load_queries
from litsearch.profiles import DEFAULT_PROFILE, PROFILES

app = typer.Typer(
    help="Deep, multi-source, auditable literature search.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"litsearch {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Deep, multi-source, auditable literature search."""


# ------------------------------------------------------------------------- helpers


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code)


def _read_queries(queries: list[str] | None, queries_file: Path | None) -> list[str]:
    found = [q for q in (queries or []) if q.strip()]
    if queries_file is not None:
        try:
            lines = queries_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise _fail(f"cannot read {queries_file}: {exc.strerror}") from None
        found += [
            line.strip() for line in lines if line.strip() and not line.startswith("#")
        ]
    return found


def _parse_sources(text: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(s.strip() for s in text.split(",") if s.strip()))
    unknown = [n for n in names if n not in SOURCE_NAMES]
    if unknown or not names:
        raise _fail(f"--sources must list some of {', '.join(SOURCE_NAMES)}")
    return names


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"about {max(1, round(seconds))} s"
    return f"about {round(seconds / 60)} min"


def _plan_lines(
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
        f"{len(params.queries)} queries, sources {', '.join(params.sources)}{modes}, "
        f"up to {params.per_query} papers per query and source, "
        f"profile {params.profile}, {limit}",
        f"Requests: {requests} ({est.total_requests} in total)",
        f"Time: {_duration(est.seconds)} (sources run side by side)",
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


def _result_lines(summary: export.RunSummary, result: RetrievalResult) -> list[str]:
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


async def _session(
    conn: sqlite3.Connection,
    settings: Settings,
    params: RunParams,
    run_id: int | None,
    plan_texts: list[tuple[int, str]],
    *,
    dry_run: bool,
    yes: bool,
    max_minutes: float | None,
    name: str | None,
) -> tuple[int, RetrievalResult] | None:
    """Show the estimate, ask for confirmation, then run. ``None`` after a dry run."""
    plan = plan_fetches(plan_texts, params.sources, params.openalex_mode)
    done = done_fetch_keys(conn, run_id) if run_id is not None else set()
    est = estimate(plan, params.per_query, done)
    async with build_client(settings) as client:
        collectors: dict[str, Any] = build_collectors(client, settings, params.sources)
        quota = None
        fetch_quota = getattr(collectors.get("openalex"), "fetch_quota", None)
        if callable(fetch_quota):
            try:
                quota = await fetch_quota()
            except CollectorError:
                quota = None  # the estimate is still useful without it
        for line in _plan_lines(params, est, quota, resuming=run_id is not None):
            typer.echo(line)
        if dry_run:
            typer.echo("Dry run: nothing was fetched and nothing was stored.")
            return None
        if not yes and not typer.confirm("Proceed?", default=False):
            typer.echo("Aborted. Nothing was fetched.")
            raise typer.Exit(1)
        if run_id is None:
            try:
                run_id = create_run(conn, params, name)
            except sqlite3.IntegrityError:
                raise _fail(f"a run named {name!r} already exists") from None
        result = await execute_run(
            conn,
            run_id,
            collectors,
            max_minutes=max_minutes,
            progress=typer.echo,
        )
        return run_id, result


# ------------------------------------------------------------------------- commands


@app.command()
def run(
    query: Annotated[
        list[str] | None,
        typer.Option("--query", "-q", help="Search phrase; repeat for several."),
    ] = None,
    queries_file: Annotated[
        Path | None,
        typer.Option(help="Text file with one query per line (# starts a comment)."),
    ] = None,
    profile: Annotated[
        str, typer.Option(help="Size of the search: quick, standard or deep.")
    ] = DEFAULT_PROFILE,
    per_query: Annotated[
        int | None,
        typer.Option(help="Papers requested per query and source (profile default)."),
    ] = None,
    sources: Annotated[
        str, typer.Option(help="Comma-separated: openalex, s2, arxiv.")
    ] = ",".join(SOURCE_NAMES),
    openalex_mode: Annotated[
        str, typer.Option(help="OpenAlex search: title-abstract, fulltext or both.")
    ] = "title-abstract",
    since: Annotated[
        str | None, typer.Option(help="Only papers on or after this date (YYYY-MM-DD).")
    ] = None,
    max_minutes: Annotated[
        float | None,
        typer.Option(
            help="Stop after this many minutes, 0 for no limit (profile default)."
        ),
    ] = None,
    top: Annotated[int, typer.Option(help="Candidates listed in candidates.md.")] = 50,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be done; fetch nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Do not ask for confirmation.")
    ] = False,
    resume: Annotated[
        int | None, typer.Option(help="Continue the run with this id.")
    ] = None,
    name: Annotated[
        str | None, typer.Option(help="Name of the run (and of its output folder).")
    ] = None,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite file (default: <DATA_DIR>/litsearch.db)."),
    ] = None,
    out_dir: Annotated[Path, typer.Option(help="Where results are written.")] = Path(
        "out"
    ),
) -> None:
    """Search the sources for the given queries and list the candidate papers."""
    settings = Settings.from_env(env_file=Path(".env"))
    database = db_path or settings.data_dir / "litsearch.db"

    conn: sqlite3.Connection | None = None
    run_id: int | None = resume
    if resume is not None:
        if query or queries_file:
            raise _fail("--resume continues a stored run; do not give new queries")
        if not database.exists():
            raise _fail(f"no database at {database}")
        conn = db.connect(database)
        db.init_schema(conn)
        try:
            params = load_params(conn, resume)
        except LookupError as exc:
            raise _fail(str(exc)) from None
        plan_texts = load_queries(conn, resume)
    else:
        if profile not in PROFILES:
            raise _fail(f"--profile must be one of {', '.join(PROFILES)}")
        if openalex_mode not in OPENALEX_MODES:
            raise _fail(f"--openalex-mode must be one of {', '.join(OPENALEX_MODES)}")
        queries = list(
            dict.fromkeys(
                " ".join(q.split()) for q in _read_queries(query, queries_file)
            )
        )
        if not queries:
            raise _fail("give at least one query with --query or --queries-file")
        chosen = PROFILES[profile]
        size = per_query if per_query is not None else chosen.per_query
        if not 1 <= size <= MAX_PER_QUERY:
            raise _fail(f"--per-query must be between 1 and {MAX_PER_QUERY}")
        try:
            since_date = date.fromisoformat(since) if since else None
        except ValueError:
            raise _fail("--since must look like 2024-01-31") from None
        minutes = chosen.max_minutes if max_minutes is None else max_minutes
        params = RunParams(
            queries=tuple(queries),
            sources=_parse_sources(sources),
            per_query=size,
            openalex_mode=openalex_mode,
            profile=profile,
            since=since_date.isoformat() if since_date else None,
            max_minutes=minutes or None,
        )
        plan_texts = list(
            enumerate(params.queries, start=1)
        )  # ids only matter once stored
        if not dry_run:
            database.parent.mkdir(parents=True, exist_ok=True)
            conn = db.connect(database)
            db.init_schema(conn)
    if conn is None:  # a dry run without --resume does not touch the database
        conn = db.connect(":memory:")
        db.init_schema(conn)

    try:
        outcome = asyncio.run(
            _session(
                conn,
                settings,
                params,
                run_id,
                plan_texts,
                dry_run=dry_run,
                yes=yes,
                max_minutes=max_minutes if resume is not None else None,
                name=name,
            )
        )
    except KeyboardInterrupt:
        typer.echo("\nInterrupted. What was fetched is saved.", err=True)
        if run_id is not None:
            typer.echo(f"Continue with: litsearch run --resume {run_id}", err=True)
        raise typer.Exit(130) from None
    if outcome is None:
        return

    run_id, result = outcome
    folder = out_dir / run_name(conn, run_id)
    candidates = export.load_candidates(conn, run_id)
    summary = export.load_summary(conn, run_id)
    export.write_csv(candidates, folder / "candidates.csv")
    export.write_markdown(candidates, summary, folder / "candidates.md", top=top)
    typer.echo("")
    for line in _result_lines(summary, result):
        typer.echo(line)
    typer.echo(f"Files: {folder / 'candidates.csv'} and {folder / 'candidates.md'}")


@app.command()
def status(
    run_id: Annotated[
        int | None, typer.Argument(help="Run id (default: the latest run).")
    ] = None,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite file (default: <DATA_DIR>/litsearch.db)."),
    ] = None,
) -> None:
    """Show what a run did: its sources, queries and how many papers it found."""
    settings = Settings.from_env(env_file=Path(".env"))
    database = db_path or settings.data_dir / "litsearch.db"
    if not database.exists():
        raise _fail(f"no database at {database}; run a search first", code=1)
    conn = db.connect(database)
    db.init_schema(conn)
    chosen = run_id if run_id is not None else export.latest_run_id(conn)
    if chosen is None:
        raise _fail("there are no runs yet", code=1)
    try:
        summary = export.load_summary(conn, chosen)
    except LookupError as exc:
        raise _fail(str(exc), code=1) from None
    typer.echo(f"{summary.name} (id {summary.run_id}): {summary.status}")
    typer.echo(f"Started {summary.started_at}, finished {summary.finished_at or '-'}")
    typer.echo(f"{summary.n_queries} queries, {summary.n_papers} distinct papers")
    typer.echo("Sources:")
    for s in summary.sources:
        note = f"  ({s.error})" if s.error else ""
        typer.echo(
            f"  {s.source:9} {s.status:13} {s.n_requests:3} requests "
            f"{s.n_results:4} results{note}"
        )
    typer.echo("Queries (results per source):")
    for text, counts in summary.queries:
        found = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
        typer.echo(f"  {text!r}: {found}")


@app.command("check-llm")
def check_llm(
    list_models: Annotated[
        bool,
        typer.Option("--list-models", help="Always print the endpoint's model list."),
    ] = False,
) -> None:
    """Check the model settings in .env with a few tiny requests."""
    settings = Settings.from_env(env_file=Path(".env"))
    if not settings.llm_api_key:
        raise _fail("LLM_API_KEY is empty; set it in .env (see .env.example)")
    failed = asyncio.run(_check_llm(settings, list_models))
    if failed:
        raise typer.Exit(1)


async def _check_llm(settings: Settings, always_list: bool) -> bool:
    """Returns True if anything failed."""
    llm = build_llm(settings)
    try:
        if settings.llm_base_url:
            parts = urlparse(settings.llm_base_url)
            typer.echo(
                f"Endpoint: {parts.scheme}://{parts.netloc} (relay or custom API)"
            )
        else:
            typer.echo("Endpoint: the official API of each format")
        typer.echo(f"API format setting: {settings.llm_api_format}")
        models = {
            "L1_MODEL": settings.l1_model,
            "QUERY_MODEL": settings.query_model,
        }
        failed = False
        for variable, model in models.items():
            if not model:
                typer.echo(f"  {variable:11} (empty)")
                failed = True
                continue
            fmt = llm.format_for(model)
            started = time.monotonic()
            try:
                reply = await llm.complete(
                    model=model, user="Reply with the single word: OK", max_tokens=64
                )
            except LLMError as exc:
                typer.echo(
                    f"  {variable:11} {model}: {fmt} format, "
                    f"FAILED ({exc.kind}): {exc.message}"
                )
                failed = True
                continue
            took = time.monotonic() - started
            note = ""
            if not reply.text.strip():
                note = (
                    " - empty reply (cut off: the model may spend its tokens "
                    "on reasoning)"
                )
            typer.echo(
                f"  {variable:11} {model}: {fmt} format, OK in {took:.1f} s, "
                f"reply {reply.text.strip()[:20]!r}, served by {reply.model!r}, "
                f"{reply.input_tokens} in / {reply.output_tokens} out tokens{note}"
            )
        if failed or always_list:
            names = await llm.list_models()
            if names:
                typer.echo(f"\nThe endpoint lists {len(names)} models:")
                typer.echo("  " + ", ".join(names))
            else:
                typer.echo("\nThe endpoint does not provide a model list.")
        return failed
    finally:
        await llm.aclose()
