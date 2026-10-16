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
from litsearch.pdf import PdfError, read_opening
from litsearch.pipeline import export
from litsearch.pipeline.brief import make_brief
from litsearch.pipeline.outputs import write_outputs
from litsearch.pipeline.queries import (
    NotCachedError,
    QueryPlan,
    generate_queries,
    write_queries_file,
)
from litsearch.pipeline.retrieve import (
    MAX_PER_QUERY,
    OPENALEX_MODES,
    RetrievalResult,
    estimate,
    plan_fetches,
)
from litsearch.pipeline.runner import (
    RunParams,
    create_run,
    execute_run,
    load_params,
)
from litsearch.pipeline.score import (
    DEFAULT_BATCH_SIZE,
    ScoreResult,
    plan_scoring,
    score_run,
)
from litsearch.pipeline.store import done_fetch_keys, load_queries
from litsearch.profiles import DEFAULT_PROFILE, PROFILES
from litsearch.reporting import (
    ScoringOptions,
)
from litsearch.reporting import (
    plan_lines as _plan_lines,
)
from litsearch.reporting import (
    queries_phrase as _queries,
)
from litsearch.reporting import (
    result_lines as _result_lines,
)
from litsearch.reporting import (
    score_lines as _score_lines,
)
from litsearch.reporting import (
    scoring_options as _build_scoring_options,
)
from litsearch.reporting import (
    scoring_plan_lines as _scoring_plan_lines,
)

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


def _scoring_options(*args: Any, **kwargs: Any) -> ScoringOptions:
    """Validated scoring options; a bad value becomes a readable command-line error."""
    try:
        return _build_scoring_options(*args, **kwargs)
    except ValueError as exc:
        raise _fail(str(exc)) from None


async def _score(
    conn: sqlite3.Connection,
    settings: Settings,
    run_id: int,
    opts: ScoringOptions,
    deadline: float | None,
) -> ScoreResult:
    llm = build_llm(settings)
    try:
        return await score_run(
            conn,
            llm,
            run_id,
            model=opts.model,
            language=settings.summary_language,
            max_scored=opts.max_scored,
            max_llm_calls=opts.max_llm_calls,
            batch_size=opts.batch_size,
            deadline=deadline,
            progress=typer.echo,
        )
    finally:
        await llm.aclose()


_write_outputs = write_outputs


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
    scoring: ScoringOptions,
) -> tuple[int, RetrievalResult, ScoreResult | None] | None:
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
        already = None
        if run_id is not None and scoring.enabled:
            sp = plan_scoring(
                conn,
                run_id,
                model=scoring.model,
                language=settings.summary_language,
                max_scored=scoring.max_scored,
            )
            already = (sp.n_to_score, sp.n_cached)
        for line in _scoring_plan_lines(scoring, est.max_records, already):
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
        started = time.monotonic()
        result = await execute_run(
            conn,
            run_id,
            collectors,
            max_minutes=max_minutes,
            progress=typer.echo,
        )
        score_result = None
        if scoring.enabled:
            limit = max_minutes if max_minutes is not None else params.max_minutes
            deadline = started + limit * 60 if limit else None
            score_result = await _score(conn, settings, run_id, scoring, deadline)
        return run_id, result, score_result


def _open_db(settings: Settings, db_path: Path | None) -> sqlite3.Connection:
    database = db_path or settings.data_dir / "litsearch.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(database)
    db.init_schema(conn)
    return conn


def _read_text(path: Path, what: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail(f"cannot read {what} {path}: {exc.strerror}") from None
    if not text.strip():
        raise _fail(f"{what} {path} is empty")
    return text


def _require_models(settings: Settings, *, need_key: bool = True) -> str:
    """The query model name, after checking the model settings are filled in."""
    if need_key and not settings.llm_api_key:
        raise _fail("LLM_API_KEY is empty; set it in .env (see .env.example)")
    if not settings.query_model:
        raise _fail("QUERY_MODEL is empty; set it in .env (see .env.example)")
    return settings.query_model


def _model_call(call: Any) -> Any:
    """Run an async model call, turning model errors into a readable message."""
    try:
        return asyncio.run(call)
    except LLMError as exc:
        raise _fail(
            f"model request failed ({exc.kind}): {exc.message}", code=1
        ) from None


def _print_query_plan(plan: QueryPlan) -> None:
    where = (
        "from the cache, no model call"
        if plan.from_cache
        else f"{plan.input_tokens} in / {plan.output_tokens} out tokens"
    )
    typer.echo(f"{_queries(len(plan.queries))} (model {plan.model}; {where}):")
    for index, q in enumerate(plan.queries, start=1):
        typer.echo(f"  {index:2}. {q.text}" + (f"  - {q.angle}" if q.angle else ""))
    if plan.n_rejected:
        typer.echo(
            f"Note: {plan.n_rejected} unusable or repeated entries were skipped."
        )
    if plan.n_over_ceiling:
        typer.echo(
            f"Note: the model wrote {plan.n_over_ceiling} more queries than the "
            "ceiling allows; they were dropped."
        )


async def _generate(
    settings: Settings,
    conn: sqlite3.Connection,
    model: str,
    project_text: str,
    max_queries: int,
    cache_only: bool,
) -> QueryPlan:
    llm = None if cache_only else build_llm(settings)
    try:
        return await generate_queries(
            llm,
            conn,
            model=model,
            project=project_text,
            language=settings.summary_language,
            max_queries=max_queries,
            cache_only=cache_only,
        )
    finally:
        if llm is not None:
            await llm.aclose()


def _queries_from_project(
    settings: Settings,
    conn: sqlite3.Connection,
    project_text: str,
    max_queries: int,
    dry_run: bool,
) -> list[str]:
    model = _require_models(settings, need_key=not dry_run)
    try:
        plan = _model_call(
            _generate(settings, conn, model, project_text, max_queries, dry_run)
        )
    except NotCachedError:
        raise _fail(
            "no queries were written for this project yet, and a dry run makes no "
            "model call; run `litsearch queries --project FILE` first",
            code=1,
        ) from None
    _print_query_plan(plan)
    return [q.text for q in plan.queries]


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
        str, typer.Option(help="Size of the search: test, quick, standard or deep.")
    ] = DEFAULT_PROFILE,
    per_query: Annotated[
        int | None,
        typer.Option(help="Papers requested per query and source (profile default)."),
    ] = None,
    sources: Annotated[
        str | None,
        typer.Option(
            help="Comma-separated: openalex, s2, arxiv (profile default: all, "
            "but only arxiv for the test profile)."
        ),
    ] = None,
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
    top: Annotated[int, typer.Option(help="Papers listed in the main list.")] = 50,
    no_score: Annotated[
        bool, typer.Option("--no-score", help="Do not score papers with the model.")
    ] = False,
    max_scored: Annotated[
        int | None,
        typer.Option(help="Most papers the model scores (profile default)."),
    ] = None,
    max_llm_calls: Annotated[
        int | None, typer.Option(help="Most model calls for scoring.")
    ] = None,
    score_batch: Annotated[
        int, typer.Option(help="Papers per scoring call (1 to 30).")
    ] = DEFAULT_BATCH_SIZE,
    min_score: Annotated[
        int, typer.Option(help="Score (0 to 5) from which a paper is in the main list.")
    ] = export.DEFAULT_MIN_SCORE,
    maybe_score: Annotated[
        int, typer.Option(help="Score from which a paper is 'possibly relevant'.")
    ] = export.DEFAULT_MAYBE_SCORE,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be done; fetch nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Do not ask for confirmation.")
    ] = False,
    project: Annotated[
        Path | None,
        typer.Option(
            help="Project brief (see `litsearch brief`); the model writes the queries."
        ),
    ] = None,
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
        if query or queries_file or project:
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
        project_text: str | None = None
        origin = "user"
        if project is not None:
            if query or queries_file:
                raise _fail("give either --project or --query/--queries-file, not both")
            project_text = _read_text(project, "project brief")
            if not dry_run or database.exists():
                conn = _open_db(settings, db_path)
            else:  # a dry run must not create the database: use a throw-away one
                conn = db.connect(":memory:")
                db.init_schema(conn)
            queries = _queries_from_project(
                settings, conn, project_text, PROFILES[profile].max_queries, dry_run
            )
            origin = "llm"
        else:
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
            sources=(
                _parse_sources(sources)
                if sources is not None
                else chosen.sources or tuple(SOURCE_NAMES)
            ),
            per_query=size,
            openalex_mode=openalex_mode,
            profile=profile,
            since=since_date.isoformat() if since_date else None,
            max_minutes=minutes or None,
            project=project_text,
            query_origin=origin,
        )
        plan_texts = list(
            enumerate(params.queries, start=1)
        )  # ids only matter once stored
        if not dry_run and conn is None:
            conn = _open_db(settings, db_path)
    if conn is None:  # a dry run without --resume does not touch the database
        conn = db.connect(":memory:")
        db.init_schema(conn)

    scoring = _scoring_options(
        settings,
        params.profile,
        no_score=no_score,
        max_scored=max_scored,
        max_llm_calls=max_llm_calls,
        batch_size=score_batch,
        min_score=min_score,
        maybe_score=maybe_score,
    )
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
                scoring=scoring,
            )
        )
    except KeyboardInterrupt:
        typer.echo("\nInterrupted. What was fetched is saved.", err=True)
        if run_id is not None:
            typer.echo(f"Continue with: litsearch run --resume {run_id}", err=True)
        raise typer.Exit(130) from None
    if outcome is None:
        return

    run_id, result, score_result = outcome
    summary, folder = _write_outputs(
        conn,
        run_id,
        out_dir,
        top=top,
        min_score=scoring.min_score,
        maybe_score=scoring.maybe_score,
        prefer_model=scoring.model or None,
    )
    typer.echo("")
    for line in _result_lines(summary, result):
        typer.echo(line)
    if score_result is not None:
        for line in _score_lines(score_result, scoring, run_id):
            typer.echo(line)
    typer.echo(f"Open the list in your browser: {folder / 'candidates.html'}")
    typer.echo(
        f"Also written: {folder / 'candidates.md'} (Markdown) and "
        f"{folder / 'candidates.csv'} (every paper, for a spreadsheet)"
    )


@app.command()
def brief(
    about: Annotated[
        str | None, typer.Option(help="Your research interest, in your own words.")
    ] = None,
    about_file: Annotated[
        Path | None, typer.Option(help="Read the description from this text file.")
    ] = None,
    pdf: Annotated[
        list[Path] | None,
        typer.Option(
            help="A paper you are interested in (repeatable). Its first pages are "
            "read as text; the PDF itself is not uploaded."
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Where to write the brief.")] = Path(
        "project.md"
    ),
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite the brief if it exists.")
    ] = False,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite file (default: <DATA_DIR>/litsearch.db)."),
    ] = None,
) -> None:
    """Turn your description into a project brief that you can read and edit."""
    settings = Settings.from_env(env_file=Path(".env"))
    model = _require_models(settings)
    parts = [
        t for t in (about, _read_text(about_file, "file") if about_file else None) if t
    ]
    excerpts: list[tuple[str, str]] = []
    for path in pdf or []:
        try:
            opening = read_opening(path)
        except PdfError as exc:
            raise _fail(str(exc)) from None
        cut = ", cut to the character limit" if opening.truncated else ""
        typer.echo(
            f"Read {opening.pages_read} of {opening.n_pages} pages of {opening.name} "
            f"({len(opening.text)} characters{cut})."
        )
        excerpts.append((opening.name, opening.text))
    if not parts and not excerpts:
        raise _fail(
            'describe your research with --about "...", --about-file FILE or --pdf FILE'
        )
    if out.exists() and not force:
        raise _fail(f"{out} already exists; use --force to overwrite it or --out NAME")
    conn = _open_db(settings, db_path)

    async def write() -> Any:
        llm = build_llm(settings)
        try:
            return await make_brief(
                llm,
                conn,
                model=model,
                about="\n\n".join(parts),
                language=settings.summary_language,
                excerpts=excerpts,
            )
        finally:
            await llm.aclose()

    result = _model_call(write())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result.text + "\n", encoding="utf-8")
    where = (
        "from the cache, no model call"
        if result.from_cache
        else f"{result.input_tokens} in / {result.output_tokens} out tokens"
    )
    typer.echo(result.text)
    typer.echo(f"\nBrief written to {out} (model {result.model}; {where}).")
    typer.echo("Read it, edit it if the model misunderstood something, then run:")
    typer.echo(f"  litsearch run --project {out}")


@app.command()
def queries(
    project: Annotated[
        Path, typer.Option(help="Project brief (see `litsearch brief`).")
    ],
    max_queries: Annotated[
        int | None,
        typer.Option(help="Safety ceiling on the number of queries (profile default)."),
    ] = None,
    profile: Annotated[
        str, typer.Option(help="Profile that sets the default ceiling.")
    ] = DEFAULT_PROFILE,
    out: Annotated[Path, typer.Option(help="Where to write the queries.")] = Path(
        "queries.txt"
    ),
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite the queries file if it exists.")
    ] = False,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite file (default: <DATA_DIR>/litsearch.db)."),
    ] = None,
) -> None:
    """Let the model write search queries for a project brief (you can edit them)."""
    settings = Settings.from_env(env_file=Path(".env"))
    model = _require_models(settings)
    if profile not in PROFILES:
        raise _fail(f"--profile must be one of {', '.join(PROFILES)}")
    ceiling = max_queries if max_queries is not None else PROFILES[profile].max_queries
    if ceiling < 1:
        raise _fail("--max-queries must be at least 1")
    project_text = _read_text(project, "project brief")
    if out.exists() and not force:
        raise _fail(f"{out} already exists; use --force to overwrite it or --out NAME")
    conn = _open_db(settings, db_path)
    plan = _model_call(_generate(settings, conn, model, project_text, ceiling, False))
    _print_query_plan(plan)
    write_queries_file(plan, out, force=force)
    typer.echo(f"\nWritten to {out}. Edit it if you like, then run one of:")
    typer.echo(f"  litsearch run --queries-file {out}")
    typer.echo(f"  litsearch run --project {project}   (uses the same queries)")


@app.command()
def score(
    run_id: Annotated[int, typer.Argument(help="Id of the run to score.")],
    max_scored: Annotated[
        int | None,
        typer.Option(help="Most papers the model scores (the run's profile default)."),
    ] = None,
    max_llm_calls: Annotated[
        int | None, typer.Option(help="Most model calls for scoring.")
    ] = None,
    score_batch: Annotated[
        int, typer.Option(help="Papers per scoring call (1 to 30).")
    ] = DEFAULT_BATCH_SIZE,
    min_score: Annotated[
        int, typer.Option(help="Score (0 to 5) from which a paper is in the main list.")
    ] = export.DEFAULT_MIN_SCORE,
    maybe_score: Annotated[
        int, typer.Option(help="Score from which a paper is 'possibly relevant'.")
    ] = export.DEFAULT_MAYBE_SCORE,
    top: Annotated[int, typer.Option(help="Papers listed in the main list.")] = 50,
    yes: Annotated[
        bool, typer.Option("--yes", help="Do not ask for confirmation.")
    ] = False,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite file (default: <DATA_DIR>/litsearch.db)."),
    ] = None,
    out_dir: Annotated[Path, typer.Option(help="Where results are written.")] = Path(
        "out"
    ),
) -> None:
    """Score the papers of a run that are not scored yet and rewrite its lists.

    Scores are cached, so this also rewrites the lists with other thresholds at no cost.
    """
    settings = Settings.from_env(env_file=Path(".env"))
    database = db_path or settings.data_dir / "litsearch.db"
    if not database.exists():
        raise _fail(f"no database at {database}", code=1)
    conn = db.connect(database)
    db.init_schema(conn)
    try:
        params = load_params(conn, run_id)
    except LookupError as exc:
        raise _fail(str(exc), code=1) from None
    opts = _scoring_options(
        settings,
        params.profile,
        no_score=False,
        max_scored=max_scored,
        max_llm_calls=max_llm_calls,
        batch_size=score_batch,
        min_score=min_score,
        maybe_score=maybe_score,
    )
    if not opts.enabled:
        raise _fail(f"cannot score: {opts.off_reason}")
    plan = plan_scoring(
        conn,
        run_id,
        model=opts.model,
        language=settings.summary_language,
        max_scored=opts.max_scored,
    )
    result: ScoreResult | None = None
    if plan.n_to_score:
        for line in _scoring_plan_lines(
            opts, plan.n_to_score, (plan.n_to_score, plan.n_cached)
        ):
            typer.echo(line)
        if not yes and not typer.confirm("Proceed?", default=False):
            typer.echo("Aborted. Nothing was scored.")
            raise typer.Exit(1)
        try:
            result = _model_call(_score(conn, settings, run_id, opts, None))
        except KeyboardInterrupt:
            typer.echo("\nInterrupted. What was scored is saved.", err=True)
            raise typer.Exit(130) from None
    else:
        typer.echo(
            f"Nothing to score: {plan.n_cached} of the first {plan.n_considered} "
            "papers already have a score. Rewriting the lists."
        )
    summary, folder = _write_outputs(
        conn,
        run_id,
        out_dir,
        top=top,
        min_score=opts.min_score,
        maybe_score=opts.maybe_score,
        prefer_model=opts.model,
    )
    if result is not None:
        for line in _score_lines(result, opts, run_id):
            typer.echo(line)
    typer.echo(f"{summary.n_scored} of {summary.n_papers} papers have a score.")
    typer.echo(f"Open the list in your browser: {folder / 'candidates.html'}")
    typer.echo(
        f"Also written: {folder / 'candidates.md'} (Markdown) and "
        f"{folder / 'candidates.csv'} (every paper, for a spreadsheet)"
    )


@app.command()
def report(
    run_id: Annotated[int, typer.Argument(help="Id of the run.")],
    use_model: Annotated[
        str | None,
        typer.Option(
            help="Show the scores of this model (default: the most complete scoring)."
        ),
    ] = None,
    min_score: Annotated[
        int, typer.Option(help="Score (0 to 5) from which a paper is relevant.")
    ] = export.DEFAULT_MIN_SCORE,
    maybe_score: Annotated[
        int, typer.Option(help="Score from which a paper is 'possibly relevant'.")
    ] = export.DEFAULT_MAYBE_SCORE,
    top: Annotated[
        int, typer.Option(help="Relevant papers per query in the Markdown list.")
    ] = 50,
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite file (default: <DATA_DIR>/litsearch.db)."),
    ] = None,
    out_dir: Annotated[Path, typer.Option(help="Where results are written.")] = Path(
        "out"
    ),
) -> None:
    """Rewrite the lists of a run from what is stored. Never calls a model."""
    settings = Settings.from_env(env_file=Path(".env"))
    database = db_path or settings.data_dir / "litsearch.db"
    if not database.exists():
        raise _fail(f"no database at {database}", code=1)
    if not 0 <= maybe_score <= min_score <= 5:
        raise _fail("--min-score and --maybe-score must satisfy 0 <= maybe <= min <= 5")
    conn = db.connect(database)
    db.init_schema(conn)
    try:
        summary, folder = _write_outputs(
            conn,
            run_id,
            out_dir,
            top=top,
            min_score=min_score,
            maybe_score=maybe_score,
            prefer_model=use_model,
        )
    except LookupError as exc:
        raise _fail(str(exc), code=1) from None
    by = f" by {summary.scored_by}" if summary.scored_by else ""
    typer.echo(
        f"Run {summary.name}: {summary.n_scored} of {summary.n_papers} papers "
        f"scored{by}."
    )
    typer.echo("No model was called.")
    typer.echo(f"Open the list in your browser: {folder / 'candidates.html'}")
    typer.echo(
        f"Also written: {folder / 'candidates.md'} (Markdown) and "
        f"{folder / 'candidates.csv'} (every paper, for a spreadsheet)"
    )


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
        summary = export.load_summary(conn, chosen, settings.l1_model)
    except LookupError as exc:
        raise _fail(str(exc), code=1) from None
    typer.echo(f"{summary.name} (id {summary.run_id}): {summary.status}")
    typer.echo(f"Started {summary.started_at}, finished {summary.finished_at or '-'}")
    typer.echo(f"{_queries(summary.n_queries)}, {summary.n_papers} distinct papers")
    by = f" (by {summary.scored_by})" if summary.scored_by else ""
    typer.echo(
        f"Scored by the model: {summary.n_scored} of {summary.n_papers} papers{by}"
    )
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


@app.command("ui")
def ui(
    port: Annotated[int, typer.Option(help="Preferred port on this computer.")] = 8765,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open the browser.")
    ] = False,
    no_auto_exit: Annotated[
        bool,
        typer.Option("--no-auto-exit", help="Keep running after the page is closed."),
    ] = False,
) -> None:
    """Open the web page where everything can be done (settings, search, results)."""
    from litsearch.paths import app_home
    from litsearch.ui.app import UiApp
    from litsearch.ui.server import serve

    serve(
        UiApp(app_home()),
        port=port,
        open_browser=not no_browser,
        auto_exit=not no_auto_exit,
        announce=typer.echo,
    )


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
