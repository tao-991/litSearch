"""The logic behind the web interface, independent of HTTP (see ``server.py``).

Every public method takes and returns plain JSON-able data and raises ``UiError`` with a
message that is safe and useful to show on the page. API keys are never returned.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from litsearch import __version__, db
from litsearch.collectors import SOURCE_NAMES, build_client, build_collectors
from litsearch.collectors.base import CollectorError
from litsearch.config import Settings, parse_env_file
from litsearch.envfile import EDITABLE, SECRET, update_env_file
from litsearch.llm import LLMError, build_llm
from litsearch.paths import resolve
from litsearch.pdf import PdfError, read_opening
from litsearch.pipeline import export
from litsearch.pipeline.brief import make_brief
from litsearch.pipeline.html_report import render_html
from litsearch.pipeline.outputs import write_outputs
from litsearch.pipeline.queries import generate_queries
from litsearch.pipeline.retrieve import (
    MAX_PER_QUERY,
    OPENALEX_MODES,
    estimate,
    plan_fetches,
)
from litsearch.pipeline.runner import RunParams, create_run, execute_run, load_params
from litsearch.pipeline.score import DEFAULT_BATCH_SIZE, plan_scoring, score_run
from litsearch.profiles import DEFAULT_PROFILE, PROFILES
from litsearch.reporting import (
    ScoringOptions,
    plan_lines,
    result_lines,
    score_lines,
    scoring_options,
    scoring_plan_lines,
)
from litsearch.ui.jobs import Job, JobBusyError, JobManager

MAX_ABOUT_CHARS = 20_000
MAX_BRIEF_CHARS = 40_000
MAX_PDFS = 5
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_QUERIES = 60
UPLOAD_MAX_AGE_SECONDS = 24 * 3600


class UiError(Exception):
    """An error to show to the user; ``status`` is the HTTP status to send."""

    def __init__(
        self, message: str, status: int = 400, kind: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.kind = kind


def _text(
    payload: dict[str, Any], key: str, limit: int, *, required: bool = False
) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise UiError(f"{key} must be text")
    if len(value) > limit:
        raise UiError(f"{key} is too long (the limit is {limit} characters)")
    if required and not value.strip():
        raise UiError(f"{key} must not be empty")
    return value


def _number(
    payload: dict[str, Any], key: str, *, low: float, high: float, integer: bool = True
) -> float | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise UiError(f"{key} must be a number")
    try:
        number = float(value)
    except ValueError:
        raise UiError(f"{key} must be a number") from None
    if integer and number != int(number):
        raise UiError(f"{key} must be a whole number")
    if not low <= number <= high:
        raise UiError(f"{key} must be between {low:g} and {high:g}")
    return int(number) if integer else number


def _open_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


class UiApp:
    def __init__(
        self,
        home: Path,
        *,
        collectors_factory: Callable[..., dict[str, Any]] = build_collectors,
        llm_factory: Callable[[Settings], Any] = build_llm,
        client_factory: Callable[[Settings], Any] = build_client,
        folder_opener: Callable[[Path], None] = _open_folder,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.home = home
        self.collectors_factory = collectors_factory
        self.llm_factory = llm_factory
        self.client_factory = client_factory
        self.folder_opener = folder_opener
        self.clock = clock
        self.jobs = JobManager()
        self.last_beat = clock()
        self.quit_requested = False
        self.uploads = home / "uploads"
        self._clean_uploads()

    # ------------------------------------------------------------------ plumbing

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    def load_settings(self) -> tuple[Settings, str | None]:
        """The settings from the .env file only, and a message if they are invalid."""
        try:
            return Settings.from_env(environ={}, env_file=self.env_file), None
        except ValueError as exc:
            return Settings(), str(exc)

    def settings(self) -> Settings:
        settings, problem = self.load_settings()
        if problem:
            raise UiError(f"Fix the settings first: {problem}")
        return settings

    @property
    def out_dir(self) -> Path:
        return self.home / "out"

    def open_db(self) -> sqlite3.Connection:
        settings, _ = self.load_settings()
        folder = resolve(self.home, settings.data_dir)
        folder.mkdir(parents=True, exist_ok=True)
        conn = db.connect(folder / "litsearch.db")
        db.init_schema(conn)
        return conn

    def _model_call(self, settings: Settings, work: Callable[[Any], Any]) -> Any:
        async def go() -> Any:
            llm = self.llm_factory(settings)
            try:
                return await work(llm)
            finally:
                await llm.aclose()

        try:
            return asyncio.run(go())
        except LLMError as exc:
            raise UiError(exc.message, 502, exc.kind) from None

    def _need_models(self, settings: Settings, *, model: str | None) -> str:
        if not settings.llm_api_key:
            raise UiError("Add your API key on the Settings page first.")
        if not model:
            raise UiError("Choose the models on the Settings page first.")
        return model

    # --------------------------------------------------------------------- state

    def state(self) -> dict[str, Any]:
        settings, problem = self.load_settings()
        raw: dict[str, str] = {}
        if self.env_file.exists():
            raw = parse_env_file(self.env_file.read_text(encoding="utf-8"))
        fields: dict[str, dict[str, Any]] = {}
        for key in EDITABLE:
            if key in SECRET:
                fields[key] = {"set": bool(raw.get(key))}
            else:
                fields[key] = {"value": raw.get(key, "")}
        active = self.jobs.active()
        return {
            "version": __version__,
            "home": str(self.home),
            "settings": fields,
            "settings_problem": problem,
            "ready": {
                "models": bool(settings.llm_api_key and settings.query_model),
                "scoring": bool(settings.llm_api_key and settings.l1_model),
            },
            "defaults": {
                "summary_language": settings.summary_language,
                "profile": DEFAULT_PROFILE,
                "sources": list(SOURCE_NAMES),
                "openalex_modes": list(OPENALEX_MODES),
                "score_batch": DEFAULT_BATCH_SIZE,
                "min_score": export.DEFAULT_MIN_SCORE,
                "maybe_score": export.DEFAULT_MAYBE_SCORE,
            },
            "profiles": {
                name: {
                    "max_queries": p.max_queries,
                    "per_query": p.per_query,
                    "max_scored": p.max_scored,
                    "max_minutes": p.max_minutes,
                    "sources": list(p.sources or SOURCE_NAMES),
                }
                for name, p in PROFILES.items()
            },
            "job": active.snapshot() if active else None,
        }

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, str | None] = {}
        for key, value in (payload.get("set") or {}).items():
            if key not in EDITABLE or not isinstance(value, str):
                raise UiError(f"{key} cannot be changed here")
            if key in SECRET and not value.strip():
                continue  # an empty secret field means "leave it as it is"
            updates[key] = value.strip()
        for key in payload.get("clear") or []:
            if key not in EDITABLE:
                raise UiError(f"{key} cannot be changed here")
            updates[key] = ""
        existing = (
            parse_env_file(self.env_file.read_text(encoding="utf-8"))
            if self.env_file.exists()
            else {}
        )
        try:
            Settings.from_env(
                environ={**existing, **{k: v or "" for k, v in updates.items()}}
            )
            update_env_file(self.env_file, updates)
        except ValueError as exc:
            raise UiError(str(exc)) from None
        return self.state()

    def check_llm(self) -> dict[str, Any]:
        settings = self.settings()
        if not settings.llm_api_key:
            raise UiError("Add your API key first.")

        async def work(llm: Any) -> dict[str, Any]:
            results = []
            for variable, model in (
                ("L1_MODEL", settings.l1_model),
                ("QUERY_MODEL", settings.query_model),
            ):
                if not model:
                    results.append(
                        {
                            "variable": variable,
                            "model": "",
                            "ok": False,
                            "message": "not set",
                        }
                    )
                    continue
                started = time.monotonic()
                try:
                    reply = await llm.complete(
                        model=model,
                        user="Reply with the single word: OK",
                        max_tokens=64,
                    )
                except LLMError as exc:
                    results.append(
                        {
                            "variable": variable,
                            "model": model,
                            "format": llm.format_for(model),
                            "ok": False,
                            "message": f"{exc.kind}: {exc.message}",
                        }
                    )
                    continue
                results.append(
                    {
                        "variable": variable,
                        "model": model,
                        "format": llm.format_for(model),
                        "ok": True,
                        "message": reply.text.strip()[:40]
                        or "empty reply (this model may spend its output on reasoning)",
                        "seconds": round(time.monotonic() - started, 1),
                        "served_by": reply.model,
                    }
                )
            failed = any(not r["ok"] for r in results)
            return {
                "results": results,
                "models": await llm.list_models() if failed else [],
            }

        return self._model_call(settings, work)  # type: ignore[no-any-return]

    def models(self) -> dict[str, Any]:
        settings = self.settings()
        if not settings.llm_api_key:
            raise UiError("Add your API key first.")

        async def work(llm: Any) -> list[str]:
            return await llm.list_models()  # type: ignore[no-any-return]

        names = self._model_call(settings, work)
        return {"models": names}

    # ------------------------------------------------------------------- uploads

    def _clean_uploads(self) -> None:
        if not self.uploads.is_dir():
            return
        cutoff = time.time() - UPLOAD_MAX_AGE_SECONDS
        for file in [*self.uploads.glob("*.pdf"), *self.uploads.glob("*.name")]:
            try:
                if file.stat().st_mtime < cutoff:
                    file.unlink()
            except OSError:
                continue

    def upload_pdf(self, filename: str, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_UPLOAD_BYTES:
            raise UiError("That PDF is too large (the limit is 50 MB).", 413)
        self.uploads.mkdir(parents=True, exist_ok=True)
        pdf_id = uuid.uuid4().hex
        path = self.uploads / f"{pdf_id}.pdf"
        path.write_bytes(data)
        os.chmod(path, 0o600)
        name = Path(filename).name or "paper.pdf"
        try:
            opening = read_opening(path)
        except PdfError as exc:
            path.unlink(missing_ok=True)
            raise UiError(str(exc).replace(path.name, name)) from None
        (self.uploads / f"{pdf_id}.name").write_text(name, encoding="utf-8")
        return {
            "id": pdf_id,
            "name": name,
            "pages_read": opening.pages_read,
            "n_pages": opening.n_pages,
            "chars": len(opening.text),
            "truncated": opening.truncated,
        }

    def _excerpts(self, ids: list[Any]) -> list[tuple[str, str]]:
        if len(ids) > MAX_PDFS:
            raise UiError(f"Use at most {MAX_PDFS} PDFs.")
        found: list[tuple[str, str]] = []
        for pdf_id in ids:
            if not isinstance(pdf_id, str) or not pdf_id.isalnum():
                raise UiError("Unknown PDF; upload it again.")
            path = self.uploads / f"{pdf_id}.pdf"
            if not path.is_file():
                raise UiError("A PDF is no longer available; upload it again.")
            try:
                opening = read_opening(path)
            except PdfError as exc:
                raise UiError(str(exc)) from None
            try:
                name = (self.uploads / f"{pdf_id}.name").read_text(encoding="utf-8")
            except OSError:
                name = opening.name
            found.append((name, opening.text))
        return found

    # ------------------------------------------------------- brief and the queries

    def brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        about = _text(payload, "about", MAX_ABOUT_CHARS)
        ids = payload.get("pdf_ids") or []
        if not isinstance(ids, list):
            raise UiError("pdf_ids must be a list")
        excerpts = self._excerpts(ids)
        if not about.strip() and not excerpts:
            raise UiError("Describe your research or add a PDF first.")
        settings = self.settings()
        model = self._need_models(settings, model=settings.query_model)
        conn = self.open_db()

        async def work(llm: Any) -> Any:
            return await make_brief(
                llm,
                conn,
                model=model,
                about=about,
                language=settings.summary_language,
                excerpts=excerpts,
            )

        result = self._model_call(settings, work)
        return {
            "brief": result.text,
            "from_cache": result.from_cache,
            "model": result.model,
            "tokens": [result.input_tokens, result.output_tokens],
        }

    def queries(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = _text(payload, "project", MAX_BRIEF_CHARS, required=True)
        profile = payload.get("profile") or DEFAULT_PROFILE
        if profile not in PROFILES:
            raise UiError("Unknown profile.")
        ceiling = _number(payload, "max_queries", low=1, high=MAX_QUERIES)
        ceiling = int(ceiling) if ceiling else PROFILES[profile].max_queries
        settings = self.settings()
        model = self._need_models(settings, model=settings.query_model)
        conn = self.open_db()

        async def work(llm: Any) -> Any:
            return await generate_queries(
                llm,
                conn,
                model=model,
                project=project,
                language=settings.summary_language,
                max_queries=ceiling,
            )

        plan = self._model_call(settings, work)
        return {
            "queries": [{"query": q.text, "angle": q.angle} for q in plan.queries],
            "n_rejected": plan.n_rejected,
            "n_over_ceiling": plan.n_over_ceiling,
            "ceiling": ceiling,
            "from_cache": plan.from_cache,
            "model": plan.model,
            "tokens": [plan.input_tokens, plan.output_tokens],
        }

    # ------------------------------------------------------------ plans and runs

    def _parse_run(
        self, payload: dict[str, Any], settings: Settings
    ) -> tuple[RunParams, ScoringOptions]:
        raw_queries = payload.get("queries")
        if not isinstance(raw_queries, list):
            raise UiError("Give at least one search query.")
        queries = list(
            dict.fromkeys(
                " ".join(q.split())
                for q in raw_queries
                if isinstance(q, str) and q.strip()
            )
        )
        if not queries:
            raise UiError("Give at least one search query.")
        if len(queries) > MAX_QUERIES:
            raise UiError(f"Use at most {MAX_QUERIES} queries.")
        if any(len(q) > 200 for q in queries):
            raise UiError("A query is too long.")
        profile = payload.get("profile") or DEFAULT_PROFILE
        if profile not in PROFILES:
            raise UiError("Unknown profile.")
        chosen = PROFILES[profile]
        queries = queries[: chosen.max_queries]  # the size of the search is a ceiling
        sources = payload.get("sources") or list(chosen.sources or SOURCE_NAMES)
        if (
            not isinstance(sources, list)
            or not sources
            or any(s not in SOURCE_NAMES for s in sources)
        ):
            raise UiError("Choose at least one source.")
        mode = payload.get("openalex_mode") or "title-abstract"
        if mode not in OPENALEX_MODES:
            raise UiError("Unknown OpenAlex search mode.")
        per_query = _number(payload, "per_query", low=1, high=MAX_PER_QUERY)
        minutes = _number(payload, "max_minutes", low=0, high=600, integer=False)
        since = payload.get("since") or None
        if since is not None:
            try:
                since = date.fromisoformat(str(since)).isoformat()
            except ValueError:
                raise UiError(
                    "'Only papers since' must look like 2024-01-31."
                ) from None
        project = payload.get("project")
        params = RunParams(
            queries=tuple(queries),
            sources=tuple(dict.fromkeys(sources)),
            per_query=int(per_query) if per_query else chosen.per_query,
            openalex_mode=mode,
            profile=profile,
            since=since,
            max_minutes=(chosen.max_minutes if minutes is None else minutes) or None,
            project=project if isinstance(project, str) and project.strip() else None,
            query_origin="llm" if payload.get("origin") == "llm" else "user",
        )
        scoring = payload.get("scoring") or {}
        if not isinstance(scoring, dict):
            raise UiError("The scoring options are invalid.")
        max_scored = _number(scoring, "max_scored", low=1, high=5000)
        max_calls = _number(scoring, "max_llm_calls", low=0, high=5000)
        batch = _number(scoring, "batch_size", low=1, high=30)
        relevant = _number(scoring, "min_score", low=0, high=5)
        maybe = _number(scoring, "maybe_score", low=0, high=5)
        try:
            options = scoring_options(
                settings,
                profile,
                no_score=not scoring.get("enabled", True),
                max_scored=None if max_scored is None else int(max_scored),
                max_llm_calls=None if max_calls is None else int(max_calls),
                batch_size=DEFAULT_BATCH_SIZE if batch is None else int(batch),
                min_score=export.DEFAULT_MIN_SCORE
                if relevant is None
                else int(relevant),
                maybe_score=export.DEFAULT_MAYBE_SCORE if maybe is None else int(maybe),
            )
        except ValueError as exc:
            raise UiError(str(exc)) from None
        return params, options

    def estimate(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        params, options = self._parse_run(payload, settings)
        plan = plan_fetches(
            list(enumerate(params.queries, start=1)),
            params.sources,
            params.openalex_mode,
        )
        est = estimate(plan, params.per_query)

        async def quota() -> tuple[int | None, int | None] | None:
            async with self.client_factory(settings) as client:
                collectors = self.collectors_factory(client, settings, params.sources)
                fetch = getattr(collectors.get("openalex"), "fetch_quota", None)
                if not callable(fetch):
                    return None
                try:
                    return await fetch()  # type: ignore[no-any-return]
                except CollectorError:
                    return None  # the estimate is still useful without it

        quota_info = asyncio.run(quota())
        lines = plan_lines(params, est, quota_info, resuming=False)
        lines += scoring_plan_lines(options, est.max_records, None)
        return {
            "lines": lines,
            "requests": est.requests,
            "openalex_credits": est.openalex_credits,
            "seconds": est.seconds,
            "max_records": est.max_records,
            "quota": None
            if not quota_info
            else {"remaining": quota_info[0], "limit": quota_info[1]},
            "scoring": {
                "enabled": options.enabled,
                "off_reason": options.off_reason,
                "model": options.model,
                "max_scored": options.max_scored,
            },
        }

    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        params, options = self._parse_run(payload, settings)
        name = payload.get("name")
        conn = self.open_db()
        try:
            run_id = create_run(
                conn,
                params,
                name.strip() if isinstance(name, str) and name.strip() else None,
            )
        except sqlite3.IntegrityError:
            raise UiError("A run with that name already exists.") from None

        def work(job: Job) -> None:
            job.run_id = run_id
            job_conn = self.open_db()
            started = time.monotonic()

            async def go() -> Any:
                async with self.client_factory(settings) as client:
                    collectors = self.collectors_factory(
                        client, settings, params.sources
                    )
                    result = await execute_run(
                        job_conn, run_id, collectors, progress=job.log
                    )
                score_result = None
                if options.enabled:
                    job.log("Scoring the papers...")
                    score_result = await self._score(
                        job_conn,
                        settings,
                        run_id,
                        options,
                        params.max_minutes,
                        started,
                        job,
                    )
                return result, score_result

            try:
                result, score_result = job.run_async(go())
            except asyncio.CancelledError:
                self._finish_outputs(job, job_conn, run_id, options)
                raise
            self._finish_outputs(job, job_conn, run_id, options, result, score_result)

        return self._start("run", work, job_run_id=run_id)

    def start_score(self, run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        conn = self.open_db()
        try:
            params = load_params(conn, run_id)
        except LookupError:
            raise UiError("No such run.", 404) from None
        merged = {"queries": list(params.queries), "profile": params.profile, **payload}
        _, options = self._parse_run(merged, settings)
        if not options.enabled:
            raise UiError(f"Cannot score: {options.off_reason}")

        def work(job: Job) -> None:
            job.run_id = run_id
            job_conn = self.open_db()
            plan = plan_scoring(
                job_conn,
                run_id,
                model=options.model,
                language=settings.summary_language,
                max_scored=options.max_scored,
            )
            job.log(
                f"{plan.n_to_score} papers to score ({plan.n_cached} already scored)."
            )
            score_result = job.run_async(
                self._score(
                    job_conn, settings, run_id, options, None, time.monotonic(), job
                )
            )
            self._finish_outputs(job, job_conn, run_id, options, None, score_result)

        return self._start("score", work, job_run_id=run_id)

    async def _score(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        run_id: int,
        options: ScoringOptions,
        max_minutes: float | None,
        started: float,
        job: Job,
    ) -> Any:
        llm = self.llm_factory(settings)
        try:
            return await score_run(
                conn,
                llm,
                run_id,
                model=options.model,
                language=settings.summary_language,
                max_scored=options.max_scored,
                max_llm_calls=options.max_llm_calls,
                batch_size=options.batch_size,
                deadline=started + max_minutes * 60 if max_minutes else None,
                progress=job.log,
            )
        finally:
            await llm.aclose()

    def _finish_outputs(
        self,
        job: Job,
        conn: sqlite3.Connection,
        run_id: int,
        options: ScoringOptions,
        result: Any = None,
        score_result: Any = None,
    ) -> None:
        summary, folder = write_outputs(
            conn,
            run_id,
            self.out_dir,
            min_score=options.min_score,
            maybe_score=options.maybe_score,
            prefer_model=options.model or None,
        )
        lines = result_lines(summary, result) if result is not None else []
        if score_result is not None:
            lines += score_lines(score_result, options, run_id)
        job.result = {
            "run_id": run_id,
            "name": summary.name,
            "lines": lines,
            "folder": str(folder),
            "n_papers": summary.n_papers,
            "n_scored": summary.n_scored,
        }

    def _start(
        self, kind: str, work: Callable[[Job], None], *, job_run_id: int
    ) -> dict[str, Any]:
        try:
            job = self.jobs.start(kind, work)
        except JobBusyError:
            raise UiError("Another search is still running.", 409) from None
        job.run_id = job_run_id
        return job.snapshot()

    def job(self, job_id: str, since: int = 0) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise UiError("No such job.", 404)
        return job.snapshot(since)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise UiError("No such job.", 404)
        job.cancel()
        return job.snapshot()

    # ------------------------------------------------------------------- history

    def runs(self) -> dict[str, Any]:
        conn = self.open_db()
        out = []
        for row in conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 100"):
            summary = export.load_summary(conn, row["id"])
            out.append(
                {
                    "id": summary.run_id,
                    "name": summary.name,
                    "status": summary.status,
                    "started_at": summary.started_at,
                    "n_queries": summary.n_queries,
                    "n_papers": summary.n_papers,
                    "n_scored": summary.n_scored,
                    "scored_by": summary.scored_by,
                    "queries": [text for text, _ in summary.queries],
                }
            )
        return {"runs": out}

    @staticmethod
    def _thresholds(params: dict[str, Any]) -> tuple[int, int, str | None]:
        low = _number(params, "min", low=0, high=5)
        mid = _number(params, "maybe", low=0, high=5)
        minimum = int(low) if low is not None else export.DEFAULT_MIN_SCORE
        maybe = int(mid) if mid is not None else export.DEFAULT_MAYBE_SCORE
        if maybe > minimum:
            raise UiError(
                "The 'possibly relevant' score cannot be above the 'relevant' score."
            )
        model = params.get("model")
        return minimum, maybe, model if isinstance(model, str) and model else None

    def report_html(self, run_id: int, params: dict[str, Any]) -> str:
        minimum, maybe, model = self._thresholds(params)
        conn = self.open_db()
        try:
            candidates = export.load_candidates(conn, run_id, model)
            summary = export.load_summary(conn, run_id, model)
        except LookupError:
            raise UiError("No such run.", 404) from None
        return render_html(candidates, summary, min_score=minimum, maybe_score=maybe)

    def write_files(self, run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        minimum, maybe, model = self._thresholds(payload)
        conn = self.open_db()
        try:
            summary, folder = write_outputs(
                conn,
                run_id,
                self.out_dir,
                min_score=minimum,
                maybe_score=maybe,
                prefer_model=model,
            )
        except LookupError:
            raise UiError("No such run.", 404) from None
        return {"folder": str(folder), "name": summary.name}

    def open_folder(self, run_id: int | None) -> dict[str, Any]:
        path = self.out_dir
        if run_id is not None:
            conn = self.open_db()
            try:
                from litsearch.pipeline.runner import run_name

                path = self.out_dir / run_name(conn, run_id)
            except LookupError:
                raise UiError("No such run.", 404) from None
        path.mkdir(parents=True, exist_ok=True)
        self.folder_opener(path)
        return {"folder": str(path)}

    # ----------------------------------------------------------------- lifecycle

    def heartbeat(self) -> dict[str, Any]:
        self.last_beat = self.clock()
        return {"ok": True}

    def idle_seconds(self) -> float:
        """Seconds since the page last said hello; zero while a job runs."""
        if self.jobs.active() is not None:
            return 0.0
        return self.clock() - self.last_beat

    def request_quit(self) -> dict[str, Any]:
        self.quit_requested = True
        return {"ok": True}
