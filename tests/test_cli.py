from __future__ import annotations

from typer.testing import CliRunner

from litsearch import __version__
from litsearch.cli import app

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"litsearch {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


# ------------------------------------------------------------------ search commands

import csv  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

from litsearch import cli  # noqa: E402
from litsearch.collectors.base import CollectorError, SourceStatus  # noqa: E402
from tests.pipeline.fakes import FakeCollector, meta  # noqa: E402


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in an empty folder so no real .env or data directory is picked up."""
    monkeypatch.chdir(tmp_path)
    for var in ("CONTACT_EMAIL", "OPENALEX_API_KEY", "S2_API_KEY", "DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def use_fakes(monkeypatch: pytest.MonkeyPatch, **fakes: Any) -> dict[str, Any]:
    """Make the CLI use the given fake collectors instead of the real ones."""
    monkeypatch.setattr(
        cli,
        "build_collectors",
        lambda client, settings, sources: {s: fakes[s] for s in sources},
    )
    return fakes


def two_sources() -> dict[str, Any]:
    paper = meta(
        "Agents That Review Code", arxiv_id="2501.00001", doi="10.1000/a", venue="MSR"
    )
    preprint = meta(
        "Agents That Review Code",
        source="arxiv",
        source_id="2501.00001",
        arxiv_id="2501.00001",
        work_type="preprint",
    )
    other = meta("Trust In Automation", doi="10.1000/b", authors=("Maria Rossi",))
    return {
        "openalex": FakeCollector(
            "openalex", {"agents review": [paper, other], "trust": [other]}
        ),
        "arxiv": FakeCollector("arxiv", {"agents review": [preprint]}),
    }


BASE = ["run", "--sources", "openalex,arxiv", "--db", "data/lit.db", "--out-dir", "out"]


def test_run_needs_at_least_one_query(workdir: Path) -> None:
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 2
    assert "give at least one query" in result.output


def test_dry_run_shows_the_plan_and_touches_nothing(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = use_fakes(monkeypatch, **two_sources())
    result = runner.invoke(
        app, [*BASE, "-q", "agents review", "-q", "trust", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "2 queries, sources openalex, arxiv (title-abstract)" in out
    assert "up to 15 papers per query and source, profile quick" in out
    assert "time limit 10 min" in out
    assert "Requests: openalex 2, arxiv 2 (4 in total)" in out
    assert "OpenAlex credits needed: about 20" in out
    assert "Dry run: nothing was fetched and nothing was stored." in out
    assert all(f.calls == [] for f in fakes.values())
    assert not (workdir / "data").exists()
    assert not (workdir / "out").exists()


def test_profiles_and_options_change_the_plan(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fakes(monkeypatch, **two_sources())
    deep = runner.invoke(app, [*BASE, "-q", "a", "--profile", "deep", "--dry-run"])
    assert "up to 50 papers per query" in deep.output
    assert "time limit 60 min" in deep.output
    custom = runner.invoke(
        app,
        [
            *BASE,
            "-q",
            "a",
            "--per-query",
            "7",
            "--max-minutes",
            "0",
            "--openalex-mode",
            "both",
            "--dry-run",
        ],
    )
    assert "up to 7 papers per query" in custom.output
    assert "no time limit" in custom.output
    assert "(both)" in custom.output
    assert (
        "Requests: openalex 2, arxiv 1" in custom.output
    )  # both modes: 2 OpenAlex fetches


def test_the_openalex_quota_is_shown_and_a_shortfall_is_warned_about(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = two_sources()

    async def quota() -> tuple[int, int]:
        return 15, 1000

    fakes["openalex"].fetch_quota = quota
    use_fakes(monkeypatch, **fakes)
    result = runner.invoke(app, [*BASE, "-q", "a", "-q", "b", "--dry-run"])
    assert (
        "OpenAlex credits needed: about 20 (remaining today: 15 of 1000)"
        in result.output
    )
    assert (
        "Warning: this may use more OpenAlex credits than remain today" in result.output
    )


def test_a_run_stores_results_and_writes_the_candidate_files(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fakes(monkeypatch, **two_sources())
    result = runner.invoke(
        app, [*BASE, "-q", "agents review", "-q", "trust", "--yes", "--name", "demo"]
    )
    assert result.exit_code == 0, result.output
    assert "Run demo completed." in result.output
    assert re.search(r"openalex\s+ok\s+2 requests\s+3 results", result.output)
    assert re.search(r"arxiv\s+ok\s+2 requests\s+1 results", result.output)
    assert "Papers: 2 distinct, 2 new to the database" in result.output

    rows = list(
        csv.DictReader(
            (workdir / "out" / "demo" / "candidates.csv").open(encoding="utf-8")
        )
    )
    # "Trust In Automation" was found by two queries, "Agents That Review Code" by one
    assert [r["title"] for r in rows] == [
        "Trust In Automation",
        "Agents That Review Code",
    ]
    assert rows[1]["found_by_sources"] == "arxiv, openalex"
    markdown = (workdir / "out" / "demo" / "candidates.md").read_text(encoding="utf-8")
    assert "### 1. Trust In Automation" in markdown
    assert "### 2. Agents That Review Code" in markdown

    conn = sqlite3.connect(workdir / "data" / "lit.db")
    assert conn.execute("SELECT status, name FROM runs").fetchone() == (
        "completed",
        "demo",
    )


def test_declining_the_confirmation_fetches_nothing(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = use_fakes(monkeypatch, **two_sources())
    result = runner.invoke(app, [*BASE, "-q", "agents review"], input="n\n")
    assert result.exit_code == 1
    assert "Aborted. Nothing was fetched." in result.output
    assert all(f.calls == [] for f in fakes.values())
    conn = sqlite3.connect(workdir / "data" / "lit.db")
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_accepting_the_confirmation_runs_the_search(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fakes(monkeypatch, **two_sources())
    result = runner.invoke(app, [*BASE, "-q", "agents review"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Proceed?" in result.output
    assert "completed" in result.output


def test_queries_can_come_from_a_file_with_comments(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = use_fakes(monkeypatch, **two_sources())
    (workdir / "q.txt").write_text(
        "# my queries\nagents review\n\n  trust  \nagents   review\n", encoding="utf-8"
    )
    result = runner.invoke(
        app, [*BASE, "--queries-file", "q.txt", "-q", "trust", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert [c["query"] for c in fakes["openalex"].calls] == ["trust", "agents review"]


def test_a_failing_source_is_reported_and_the_run_still_completes(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = two_sources()
    boom = CollectorError(SourceStatus.RATE_LIMITED, "arxiv: HTTP 429 after retries")
    fakes["arxiv"] = FakeCollector("arxiv", {"agents review": boom})
    use_fakes(monkeypatch, **fakes)
    result = runner.invoke(app, [*BASE, "-q", "agents review", "--yes"])
    assert result.exit_code == 0, result.output
    assert "FAILED (rate_limited)" in result.output
    assert "Run run-0001 completed." in result.output
    assert re.search(r"arxiv\s+rate_limited", result.output)
    assert "1 of 1 fetches failed" in result.output


def test_resume_repeats_only_what_failed(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = two_sources()
    fakes["arxiv"] = FakeCollector(
        "arxiv",
        {"agents review": CollectorError(SourceStatus.ERROR, "arxiv: HTTP 503")},
    )
    use_fakes(monkeypatch, **fakes)
    first = runner.invoke(app, [*BASE, "-q", "agents review", "--yes"])
    assert first.exit_code == 0

    fakes["arxiv"].script["agents review"] = two_sources()["arxiv"].script[
        "agents review"
    ]
    fakes["openalex"].calls.clear()
    fakes["arxiv"].calls.clear()
    dry = runner.invoke(
        app, ["run", "--resume", "1", "--db", "data/lit.db", "--dry-run"]
    )
    assert "Remaining work of the run" in dry.output
    assert "Requests: openalex 0, arxiv 1 (1 in total)" in dry.output

    again = runner.invoke(
        app,
        ["run", "--resume", "1", "--db", "data/lit.db", "--out-dir", "out", "--yes"],
    )
    assert again.exit_code == 0, again.output
    assert fakes["openalex"].calls == []  # already done
    assert len(fakes["arxiv"].calls) == 1
    assert re.search(r"arxiv\s+ok", again.output)
    assert "Papers: 2 distinct" in again.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--sources", "openalex,google"], "--sources must list some of"),
        (["--profile", "huge"], "--profile must be one of"),
        (["--openalex-mode", "semantic"], "--openalex-mode must be one of"),
        (["--per-query", "0"], "--per-query must be between 1 and 200"),
        (["--per-query", "500"], "--per-query must be between 1 and 200"),
        (["--since", "last week"], "--since must look like"),
        (["--queries-file", "missing.txt"], "cannot read missing.txt"),
    ],
)
def test_invalid_options_are_explained(
    workdir: Path, args: list[str], message: str
) -> None:
    result = runner.invoke(app, ["run", "-q", "a", "--dry-run", "--db", "x.db", *args])
    assert result.exit_code == 2
    assert message in result.output


def test_resume_rejects_new_queries_and_unknown_runs(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fakes(monkeypatch, **two_sources())
    runner.invoke(app, [*BASE, "-q", "agents review", "--yes"])
    clash = runner.invoke(
        app, ["run", "--resume", "1", "-q", "new", "--db", "data/lit.db"]
    )
    assert clash.exit_code == 2
    assert "do not give new queries" in clash.output
    missing = runner.invoke(
        app, ["run", "--resume", "99", "--db", "data/lit.db", "--yes"]
    )
    assert missing.exit_code == 2
    assert "no run with id 99" in missing.output
    no_db = runner.invoke(app, ["run", "--resume", "1", "--db", "nowhere/x.db"])
    assert no_db.exit_code == 2
    assert "no database at" in no_db.output


def test_a_run_name_can_only_be_used_once(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fakes(monkeypatch, **two_sources())
    assert (
        runner.invoke(
            app, [*BASE, "-q", "agents review", "--yes", "--name", "x"]
        ).exit_code
        == 0
    )
    again = runner.invoke(app, [*BASE, "-q", "agents review", "--yes", "--name", "x"])
    assert again.exit_code == 2
    assert "a run named 'x' already exists" in again.output


def test_status_describes_the_latest_or_a_chosen_run(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fakes(monkeypatch, **two_sources())
    runner.invoke(
        app, [*BASE, "-q", "agents review", "-q", "trust", "--yes", "--name", "first"]
    )
    result = runner.invoke(app, ["status", "--db", "data/lit.db"])
    assert result.exit_code == 0, result.output
    assert "first (id 1): completed" in result.output
    assert "2 queries, 2 distinct papers" in result.output
    assert "openalex" in result.output and "arxiv" in result.output
    assert "'agents review': arxiv 1, openalex 2" in result.output
    chosen = runner.invoke(app, ["status", "1", "--db", "data/lit.db"])
    assert chosen.exit_code == 0
    assert runner.invoke(app, ["status", "7", "--db", "data/lit.db"]).exit_code == 1


def test_status_without_a_database_says_so(workdir: Path) -> None:
    result = runner.invoke(app, ["status", "--db", "data/none.db"])
    assert result.exit_code == 1
    assert "run a search first" in result.output


# --------------------------------------------------------------------- check-llm

import httpx  # noqa: E402

from litsearch.llm import LLMClient  # noqa: E402

KEY = "sk-secret-key-123"


def llm_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[httpx.Request]:
    """Make the CLI talk to a scripted model endpoint; returns the requests it saw."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        cli,
        "build_llm",
        lambda settings: LLMClient(
            httpx.AsyncClient(transport=httpx.MockTransport(respond)),
            api_key=settings.llm_api_key or "",
            base_url=settings.llm_base_url,
            api_format=settings.llm_api_format,
            max_retries=0,
        ),
    )
    return seen


def anthropic_ok(model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 13, "output_tokens": 1},
        },
    )


def openai_ok(model: str, text: str = "OK") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": f"{model}-2026",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 13, "completion_tokens": 4},
        },
    )


def llm_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in {
        "LLM_API_KEY": KEY,
        "LLM_BASE_URL": "https://relay.example.com/v1",
        "L1_MODEL": "gpt-5-nano",
        "QUERY_MODEL": "claude-sonnet-5-5",
        **values,
    }.items():
        monkeypatch.setenv(name, value)


def test_check_llm_needs_a_key(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["check-llm"])
    assert result.exit_code == 2
    assert "LLM_API_KEY is empty" in result.output


def test_check_llm_reports_each_model_with_its_format(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        model = json.loads(request.content)["model"]
        return (
            openai_ok(model)
            if request.url.path.endswith("completions")
            else anthropic_ok(model)
        )

    seen = llm_endpoint(monkeypatch, handler)
    result = runner.invoke(app, ["check-llm"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Endpoint: https://relay.example.com (relay or custom API)" in out
    assert "L1_MODEL" in out and "gpt-5-nano: openai format, OK" in out
    assert "QUERY_MODEL" in out and "claude-sonnet-5-5: anthropic format, OK" in out
    assert "served by 'gpt-5-nano-2026'" in out
    assert "13 in / 4 out tokens" in out
    assert [r.url.path for r in seen] == ["/v1/chat/completions", "/v1/messages"]
    assert KEY not in out


def test_check_llm_explains_a_failure_and_lists_the_models(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_env(monkeypatch, L1_MODEL="gpt-9-imaginary")

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "gpt-5-nano"}, {"id": "claude-sonnet-5-5"}]}
            )
        model = json.loads(request.content)["model"]
        if model == "gpt-9-imaginary":
            return httpx.Response(404, json={"error": {"message": "unknown model"}})
        return anthropic_ok(model)

    llm_endpoint(monkeypatch, handler)
    result = runner.invoke(app, ["check-llm"])
    assert result.exit_code == 1
    assert "gpt-9-imaginary: openai format, FAILED (model)" in result.output
    assert "unknown model" in result.output
    assert "claude-sonnet-5-5: anthropic format, OK" in result.output
    assert "The endpoint lists 2 models:" in result.output
    assert "claude-sonnet-5-5, gpt-5-nano" in result.output


def test_check_llm_warns_about_an_empty_reply(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_env(monkeypatch, QUERY_MODEL="gpt-5-nano")
    llm_endpoint(monkeypatch, lambda request: openai_ok("gpt-5-nano", text=""))
    result = runner.invoke(app, ["check-llm"])
    assert "empty reply" in result.output


def test_check_llm_can_always_list_models_and_flags_empty_model_settings(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_env(monkeypatch, L1_MODEL="", QUERY_MODEL="")
    llm_endpoint(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5-nano"}]}),
    )
    result = runner.invoke(app, ["check-llm", "--list-models"])
    assert result.exit_code == 1  # nothing to test counts as a problem
    assert "L1_MODEL" in result.output and "(empty)" in result.output
    assert "The endpoint lists 1 models:" in result.output
