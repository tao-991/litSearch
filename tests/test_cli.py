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
    assert "Scored by the model: 0 of 2 papers" in result.output
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


# ------------------------------------------------------- brief, queries, run --project

import json  # noqa: E402

from litsearch.llm import LLMError  # noqa: E402
from tests.llm.fakes import FakeLLM  # noqa: E402

BRIEF_TEXT = "# Coding agents\n## Research question\nHow to decide faster?"


def queries_reply(*queries: str) -> str:
    return json.dumps(
        {
            "queries": [
                {"query": q, "angle": f"angle {i}"} for i, q in enumerate(queries)
            ]
        }
    )


def use_llm(monkeypatch: pytest.MonkeyPatch, llm: FakeLLM) -> FakeLLM:
    monkeypatch.setattr(cli, "build_llm", lambda settings: llm)
    return llm


def llm_ready(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for name, value in {
        "LLM_API_KEY": KEY,
        "QUERY_MODEL": "model-q",
        **extra,
    }.items():
        monkeypatch.setenv(name, value)


def test_brief_needs_model_settings_and_a_description(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert (
        "LLM_API_KEY is empty" in runner.invoke(app, ["brief", "--about", "x"]).output
    )
    monkeypatch.setenv("LLM_API_KEY", KEY)
    assert (
        "QUERY_MODEL is empty" in runner.invoke(app, ["brief", "--about", "x"]).output
    )
    llm_ready(monkeypatch)
    result = runner.invoke(app, ["brief"])
    assert result.exit_code == 2
    assert "describe your research" in result.output


def test_brief_writes_the_project_file_and_explains_the_next_step(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    llm = use_llm(monkeypatch, FakeLLM([BRIEF_TEXT]))
    result = runner.invoke(
        app, ["brief", "--about", "How can coding agents help engineers decide faster?"]
    )
    assert result.exit_code == 0, result.output
    assert (workdir / "project.md").read_text(encoding="utf-8").strip() == BRIEF_TEXT
    assert "Brief written to project.md" in result.output
    assert "litsearch run --project project.md" in result.output
    assert llm.calls[0]["model"] == "model-q"
    assert "Chinese" in llm.calls[0]["system"]  # the default summary language
    assert "decide faster" in llm.calls[0]["user"]


def test_brief_uses_the_configured_language_and_a_description_file(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch, SUMMARY_LANGUAGE="English")
    (workdir / "idea.txt").write_text("my long idea in a file", encoding="utf-8")
    llm = use_llm(monkeypatch, FakeLLM([BRIEF_TEXT]))
    result = runner.invoke(
        app,
        ["brief", "--about", "short note", "--about-file", "idea.txt", "--out", "b.md"],
    )
    assert result.exit_code == 0, result.output
    assert "in English as Markdown" in llm.calls[0]["system"]
    assert (
        "short note" in llm.calls[0]["user"] and "my long idea" in llm.calls[0]["user"]
    )
    assert (workdir / "b.md").exists()


def test_brief_never_overwrites_your_edits_without_force(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    (workdir / "project.md").write_text("my edits", encoding="utf-8")
    llm = use_llm(monkeypatch, FakeLLM([BRIEF_TEXT]))
    refused = runner.invoke(app, ["brief", "--about", "x"])
    assert refused.exit_code == 2
    assert "already exists" in refused.output
    assert llm.calls == []  # no money spent before the check
    assert (workdir / "project.md").read_text(encoding="utf-8") == "my edits"
    assert runner.invoke(app, ["brief", "--about", "x", "--force"]).exit_code == 0
    assert (workdir / "project.md").read_text(encoding="utf-8").strip() == BRIEF_TEXT


def test_brief_reports_model_failures(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    use_llm(monkeypatch, FakeLLM([LLMError("auth", "the endpoint refused the key")]))
    result = runner.invoke(app, ["brief", "--about", "x"])
    assert result.exit_code == 1
    assert "model request failed (auth)" in result.output
    assert not (workdir / "project.md").exists()


def test_queries_lists_them_with_their_angle_and_writes_the_file(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    llm = use_llm(
        monkeypatch, FakeLLM([queries_reply("code review agents", "trust automation")])
    )
    result = runner.invoke(app, ["queries", "--project", "project.md"])
    assert result.exit_code == 0, result.output
    assert "2 queries (model fake-model; 100 in / 50 out tokens):" in result.output
    assert "1. code review agents  - angle 0" in result.output
    text = (workdir / "queries.txt").read_text(encoding="utf-8")
    assert "code review agents" in text.splitlines()
    assert "15" not in llm.calls[0]["system"]  # the ceiling is not shown to the model
    assert "litsearch run --queries-file queries.txt" in result.output


def test_queries_ceiling_and_profile_options(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    twenty = queries_reply(*[f"query number {i}" for i in range(20)])
    use_llm(monkeypatch, FakeLLM([twenty]))
    quick = runner.invoke(app, ["queries", "--project", "project.md", "--out", "a.txt"])
    assert "5 queries" in quick.output  # the quick profile's ceiling
    assert "15 more queries than the ceiling" in quick.output
    limited = runner.invoke(
        app,
        ["queries", "--project", "project.md", "--max-queries", "4", "--out", "b.txt"],
    )
    assert "4 queries" in limited.output and "16 more queries" in limited.output
    deep = runner.invoke(
        app,
        ["queries", "--project", "project.md", "--profile", "deep", "--out", "c.txt"],
    )
    assert deep.exit_code == 0, deep.output
    assert "20 queries" in deep.output and "ceiling" not in deep.output
    bad = runner.invoke(
        app, ["queries", "--project", "project.md", "--max-queries", "0"]
    )
    assert bad.exit_code == 2


def test_queries_refuses_to_overwrite_and_checks_its_input(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    llm = use_llm(monkeypatch, FakeLLM([queries_reply("code review agents")]))
    missing = runner.invoke(app, ["queries", "--project", "nope.md"])
    assert missing.exit_code == 2 and "cannot read project brief" in missing.output
    (workdir / "project.md").write_text("  \n", encoding="utf-8")
    assert (
        "is empty" in runner.invoke(app, ["queries", "--project", "project.md"]).output
    )
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    (workdir / "queries.txt").write_text("my edits", encoding="utf-8")
    refused = runner.invoke(app, ["queries", "--project", "project.md"])
    assert refused.exit_code == 2 and "already exists" in refused.output
    assert llm.calls == []


def test_the_queries_file_can_be_edited_and_used_for_a_run(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    use_llm(monkeypatch, FakeLLM([queries_reply("agents review", "trust")]))
    use_fakes(monkeypatch, **two_sources())
    runner.invoke(app, ["queries", "--project", "project.md"])
    result = runner.invoke(app, [*BASE, "--queries-file", "queries.txt", "--yes"])
    assert result.exit_code == 0, result.output
    assert "2 queries, sources openalex, arxiv" in result.output


def test_run_with_a_project_asks_the_model_once_and_remembers_the_answer(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    llm = use_llm(monkeypatch, FakeLLM([queries_reply("agents review", "trust")]))
    use_fakes(monkeypatch, **two_sources())
    first = runner.invoke(
        app, [*BASE, "--project", "project.md", "--yes", "--name", "p1"]
    )
    assert first.exit_code == 0, first.output
    assert "2 queries (model fake-model; 100 in / 50 out tokens):" in first.output
    assert "Run p1 completed." in first.output
    conn = sqlite3.connect(workdir / "data" / "lit.db")
    origins = {r[0] for r in conn.execute("SELECT origin FROM queries")}
    assert origins == {"llm"}
    stored = json.loads(conn.execute("SELECT params FROM runs").fetchone()[0])
    assert stored["project"].strip() == BRIEF_TEXT
    assert stored["query_origin"] == "llm"

    second = runner.invoke(
        app, [*BASE, "--project", "project.md", "--yes", "--name", "p2"]
    )
    assert second.exit_code == 0, second.output
    assert "from the cache, no model call" in second.output
    assert len(llm.calls) == 1  # the second run did not ask again


def test_a_dry_run_with_a_project_makes_no_model_call(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUERY_MODEL", "model-q")  # note: no LLM_API_KEY at all
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    use_fakes(monkeypatch, **two_sources())
    uncached = runner.invoke(app, [*BASE, "--project", "project.md", "--dry-run"])
    assert uncached.exit_code == 1
    assert "a dry run makes no model call" in uncached.output
    assert "litsearch queries --project FILE" in uncached.output
    assert not (workdir / "data").exists()  # and the database was not created

    llm_ready(monkeypatch)  # now generate and cache the queries for real...
    use_llm(monkeypatch, FakeLLM([queries_reply("agents review", "trust")]))
    assert (
        runner.invoke(app, [*BASE, "--project", "project.md", "--yes"]).exit_code == 0
    )
    monkeypatch.delenv("LLM_API_KEY", raising=False)  # ...and dry-run without a key
    cached = runner.invoke(app, [*BASE, "--project", "project.md", "--dry-run"])
    assert cached.exit_code == 0, cached.output
    assert "from the cache, no model call" in cached.output
    assert "Dry run: nothing was fetched" in cached.output


def test_project_conflicts_and_missing_files_are_explained(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    both = runner.invoke(
        app, [*BASE, "--project", "project.md", "-q", "x", "--dry-run"]
    )
    assert both.exit_code == 2 and "either --project or --query" in both.output
    missing = runner.invoke(app, [*BASE, "--project", "nope.md", "--dry-run"])
    assert missing.exit_code == 2 and "cannot read project brief" in missing.output
    resume = runner.invoke(
        app, ["run", "--resume", "1", "--project", "project.md", "--db", "data/lit.db"]
    )
    assert resume.exit_code == 2
    assert "do not give new queries" in resume.output


def test_queries_label_the_angles_in_the_configured_language(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    llm_ready(monkeypatch)
    llm = use_llm(monkeypatch, FakeLLM([queries_reply("code review agents")]))
    runner.invoke(app, ["queries", "--project", "project.md", "--out", "a.txt"])
    assert (
        "angle" in llm.calls[0]["system"]
        and "in Chinese saying" in llm.calls[0]["system"]
    )

    llm_ready(monkeypatch, SUMMARY_LANGUAGE="Japanese")
    llm2 = use_llm(monkeypatch, FakeLLM([queries_reply("code review agents")]))
    runner.invoke(app, ["queries", "--project", "project.md", "--out", "b.txt"])
    assert "in Japanese saying" in llm2.calls[0]["system"]


def test_singular_and_plural_of_queries_in_the_output(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fakes(monkeypatch, **two_sources())
    one = runner.invoke(app, [*BASE, "-q", "agents review", "--dry-run"])
    assert "Plan: 1 query, sources" in one.output
    two = runner.invoke(app, [*BASE, "-q", "agents review", "-q", "trust", "--dry-run"])
    assert "Plan: 2 queries, sources" in two.output
    llm_ready(monkeypatch)
    (workdir / "project.md").write_text(BRIEF_TEXT, encoding="utf-8")
    use_llm(monkeypatch, FakeLLM([queries_reply("code review agents")]))
    made = runner.invoke(app, ["queries", "--project", "project.md"])
    assert "1 query (model" in made.output


# --------------------------------------------------------------------- brief --pdf

from pypdf import PdfWriter  # noqa: E402

from tests.pdf_fixtures import make_pdf  # noqa: E402


def pdf_file(workdir: Path, name: str, *pages: list[str]) -> Path:
    path = workdir / name
    path.write_bytes(make_pdf(list(pages)))
    return path


def test_brief_reads_the_opening_of_pdfs_and_passes_the_text_on(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    pdf_file(
        workdir, "hitl.pdf", ["Human-in-the-loop agents", "Abstract: we study agents."]
    )
    pdf_file(workdir, "trust.pdf", ["Trust in automation"], ["Second page text"])
    llm = use_llm(monkeypatch, FakeLLM([BRIEF_TEXT]))
    result = runner.invoke(
        app,
        ["brief", "--pdf", "hitl.pdf", "--pdf", "trust.pdf", "--about", "also trust"],
    )
    assert result.exit_code == 0, result.output
    assert "Read 1 of 1 pages of hitl.pdf" in result.output
    assert "Read 2 of 2 pages of trust.pdf" in result.output
    user = llm.calls[0]["user"]
    assert "also trust" in user
    assert "Paper 1 provided by the user (hitl.pdf)" in user
    assert "Human-in-the-loop agents" in user and "we study agents" in user
    assert "Paper 2 provided by the user (trust.pdf)" in user
    assert "Second page text" in user
    assert "If the user provided papers" in llm.calls[0]["system"]


def test_a_pdf_alone_is_enough_to_write_a_brief(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    pdf_file(workdir, "only.pdf", ["A paper I like"])
    use_llm(monkeypatch, FakeLLM([BRIEF_TEXT]))
    result = runner.invoke(app, ["brief", "--pdf", "only.pdf"])
    assert result.exit_code == 0, result.output
    assert (workdir / "project.md").exists()


def test_a_long_pdf_is_reported_as_cut(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_ready(monkeypatch)
    pdf_file(workdir, "long.pdf", [("word " * 40) for _ in range(200)])
    llm = use_llm(monkeypatch, FakeLLM([BRIEF_TEXT]))
    result = runner.invoke(app, ["brief", "--pdf", "long.pdf"])
    assert "cut to the character limit" in result.output
    assert len(llm.calls[0]["user"]) < 8000


@pytest.mark.parametrize("problem", ["missing", "not_pdf", "scanned"])
def test_an_unusable_pdf_stops_before_any_model_call(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    llm_ready(monkeypatch)
    llm = use_llm(monkeypatch, FakeLLM([BRIEF_TEXT]))
    if problem == "not_pdf":
        (workdir / "x.pdf").write_text("plain text", encoding="utf-8")
    elif problem == "scanned":
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with (workdir / "x.pdf").open("wb") as handle:
            writer.write(handle)
    result = runner.invoke(app, ["brief", "--pdf", "x.pdf", "--about", "something"])
    assert result.exit_code == 2
    assert "x.pdf" in result.output
    assert llm.calls == []
    assert not (workdir / "project.md").exists()


# ------------------------------------------------------------------------- scoring


def score_reply(*scores: int) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "id": i,
                    "score": s,
                    "query": 1,
                    "reason": f"reason {i}",
                    "summary": f"Summary {i}. Two. Three.",
                    "tags": ["tag"],
                }
                for i, s in enumerate(scores, start=1)
            ]
        }
    )


def scoring_env(monkeypatch: pytest.MonkeyPatch) -> None:
    llm_ready(monkeypatch, L1_MODEL="model-l1")


SEARCH = [*BASE, "-q", "agents review", "-q", "trust"]


def test_the_dry_run_estimates_scoring_or_says_it_is_skipped(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fakes(monkeypatch, **two_sources())
    skipped = runner.invoke(app, [*SEARCH, "--dry-run"])
    assert (
        "Scoring: skipped (LLM_API_KEY or L1_MODEL is empty in .env)" in skipped.output
    )
    assert "will not be ranked by relevance" in skipped.output
    off = runner.invoke(app, [*SEARCH, "--dry-run", "--no-score"])
    assert "Scoring: skipped (--no-score)" in off.output

    scoring_env(monkeypatch)
    planned = runner.invoke(app, [*SEARCH, "--dry-run"])
    assert planned.exit_code == 0, planned.output
    # 2 queries x (openalex 15 + arxiv 15) = 60 results at most (ceiling 150)
    assert (
        "Scoring: up to 60 papers (best ranked first) in about 6 model calls"
        in planned.output
    )
    assert "with model-l1" in planned.output
    assert "tokens in" in planned.output
    limited = runner.invoke(
        app,
        [
            *SEARCH,
            "--dry-run",
            "--max-scored",
            "20",
            "--max-llm-calls",
            "1",
            "--score-batch",
            "5",
        ],
    )
    assert (
        "up to 20 papers" in limited.output
        and "in about 1 model calls" in limited.output
    )


def test_a_run_scores_the_papers_and_ranks_the_lists(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoring_env(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    # best ranked first: "Trust In Automation" (two queries), then the agents paper
    llm = use_llm(monkeypatch, FakeLLM([score_reply(5, 3)]))
    result = runner.invoke(app, [*SEARCH, "--yes", "--name", "scored"])
    assert result.exit_code == 0, result.output
    assert (
        "Scoring: 2 papers scored by the model, 0 already scored before, 1 model calls"
        in result.output
    )
    assert "100 tokens in / 50 out" in result.output
    assert "Trust In Automation" in llm.calls[0]["user"]
    assert llm.calls[0]["model"] == "model-l1"
    assert (
        "1. agents review" in llm.calls[0]["user"]
        and "2. trust" in llm.calls[0]["user"]
    )

    markdown = (workdir / "out" / "scored" / "candidates.md").read_text(
        encoding="utf-8"
    )
    assert "Scored 2 of 2 papers from 0 to 5" in markdown
    # these fake papers have no abstract: judged by title they are capped at 4
    assert "### Trust In Automation  `4/5`" in markdown
    assert "based on the title only, no abstract" in markdown
    assert "Possibly relevant (score 3)" in markdown
    rows = list(
        csv.DictReader(
            (workdir / "out" / "scored" / "candidates.csv").open(encoding="utf-8")
        )
    )
    assert [(r["title"], r["score"]) for r in rows] == [
        ("Trust In Automation", "4"),
        ("Agents That Review Code", "3"),
    ]


def test_the_scoring_limit_is_reported(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoring_env(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    use_llm(monkeypatch, FakeLLM([score_reply(4)]))
    result = runner.invoke(app, [*SEARCH, "--yes", "--max-scored", "1"])
    assert "1 papers scored by the model" in result.output
    assert "1 more papers were found than the limit of 1 allows" in result.output
    assert "## Not scored (1)" in (
        workdir / "out" / "run-0001" / "candidates.md"
    ).read_text(encoding="utf-8")


def test_no_score_never_touches_the_model(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoring_env(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    llm = use_llm(monkeypatch, FakeLLM([]))
    result = runner.invoke(app, [*SEARCH, "--yes", "--no-score"])
    assert result.exit_code == 0, result.output
    assert llm.calls == []
    assert "not scored" in (workdir / "out" / "run-0001" / "candidates.md").read_text(
        encoding="utf-8"
    )


def test_a_scoring_failure_keeps_the_search_and_says_how_to_continue(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoring_env(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    use_llm(monkeypatch, FakeLLM([LLMError("rate_limited", "slow down")]))
    result = runner.invoke(app, [*SEARCH, "--yes"])
    assert result.exit_code == 0, result.output
    assert "Run run-0001 completed." in result.output
    assert "Stopped because of a model error (rate_limited: slow down)" in result.output
    assert "Continue with: litsearch score 1" in result.output
    assert "Papers: 2 distinct" in result.output


def test_score_adds_scores_to_a_run_and_can_be_repeated_for_free(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoring_env(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    llm = use_llm(monkeypatch, FakeLLM([score_reply(5, 2)]))
    assert runner.invoke(app, [*SEARCH, "--yes", "--no-score"]).exit_code == 0

    first = runner.invoke(
        app, ["score", "1", "--db", "data/lit.db", "--out-dir", "out", "--yes"]
    )
    assert first.exit_code == 0, first.output
    assert "Scoring: up to 2 papers" in first.output
    assert "2 papers scored by the model" in first.output
    assert "2 of 2 papers have a score." in first.output
    assert len(llm.calls) == 1

    again = runner.invoke(
        app,
        [
            "score",
            "1",
            "--db",
            "data/lit.db",
            "--out-dir",
            "out",
            "--min-score",
            "5",
            "--maybe-score",
            "2",
        ],
    )
    assert again.exit_code == 0, again.output
    assert (
        "Nothing to score: 2 of the first 2 papers already have a score."
        in again.output
    )
    assert len(llm.calls) == 1  # no second model call
    markdown = (workdir / "out" / "run-0001" / "candidates.md").read_text(
        encoding="utf-8"
    )
    assert "(score 5+)" in markdown
    assert "Possibly relevant (score 2)" in markdown


def test_score_asks_for_confirmation(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoring_env(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    llm = use_llm(monkeypatch, FakeLLM([score_reply(4, 4)]))
    runner.invoke(app, [*SEARCH, "--yes", "--no-score"])
    declined = runner.invoke(app, ["score", "1", "--db", "data/lit.db"], input="n\n")
    assert declined.exit_code == 1 and "Aborted. Nothing was scored." in declined.output
    assert llm.calls == []
    accepted = runner.invoke(
        app, ["score", "1", "--db", "data/lit.db", "--out-dir", "out"], input="y\n"
    )
    assert accepted.exit_code == 0, accepted.output
    assert len(llm.calls) == 1


def test_score_checks_its_inputs(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_db = runner.invoke(app, ["score", "1", "--db", "data/none.db"])
    assert no_db.exit_code == 1 and "no database at" in no_db.output
    use_fakes(monkeypatch, **two_sources())
    runner.invoke(app, [*SEARCH, "--yes", "--no-score"])
    unknown = runner.invoke(app, ["score", "9", "--db", "data/lit.db"])
    assert unknown.exit_code == 1 and "no run with id 9" in unknown.output
    unconfigured = runner.invoke(app, ["score", "1", "--db", "data/lit.db"])
    assert unconfigured.exit_code == 2
    assert (
        "cannot score: LLM_API_KEY or L1_MODEL is empty in .env" in unconfigured.output
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--score-batch", "0"], "--score-batch must be between 1 and 30"),
        (["--score-batch", "31"], "--score-batch must be between 1 and 30"),
        (["--min-score", "6"], "--min-score and --maybe-score must satisfy"),
        (
            ["--min-score", "2", "--maybe-score", "3"],
            "--min-score and --maybe-score must satisfy",
        ),
        (["--max-scored", "0"], "--max-scored must be at least 1"),
        (["--max-llm-calls", "-1"], "--max-llm-calls must not be negative"),
    ],
)
def test_scoring_options_are_validated(
    workdir: Path, args: list[str], message: str
) -> None:
    result = runner.invoke(app, ["run", "-q", "a", "--dry-run", "--db", "x.db", *args])
    assert result.exit_code == 2
    assert message in result.output


def test_resuming_shows_how_much_scoring_is_left(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoring_env(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    use_llm(monkeypatch, FakeLLM([score_reply(5)]))
    runner.invoke(app, [*SEARCH, "--yes", "--max-scored", "1"])
    dry = runner.invoke(
        app,
        [
            "run",
            "--resume",
            "1",
            "--db",
            "data/lit.db",
            "--dry-run",
            "--max-scored",
            "2",
        ],
    )
    assert dry.exit_code == 0, dry.output
    assert "Scoring: up to 1 papers (best ranked first, 1 already scored)" in dry.output


def test_cut_off_replies_get_a_hint_about_reasoning_models(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litsearch.llm import LLMReply

    scoring_env(monkeypatch)
    use_fakes(monkeypatch, **two_sources())
    cut = LLMReply("", "model-l1", 100, 6000, "length")
    use_llm(monkeypatch, FakeLLM([cut, cut]))
    result = runner.invoke(app, [*SEARCH, "--yes"])
    assert result.exit_code == 0, result.output
    assert "2 of 2 model replies were cut off by the output limit" in result.output
    assert "use a model without reasoning as L1_MODEL" in result.output
    assert "2 papers got no usable score" in result.output


# ----------------------------------------------------------------------------- report


def make_scored_run(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, *, model: str = "model-l1"
) -> None:
    llm_ready(monkeypatch, L1_MODEL=model)
    use_fakes(monkeypatch, **two_sources())
    use_llm(monkeypatch, FakeLLM([score_reply(5, 3)]))
    assert runner.invoke(app, [*SEARCH, "--yes", "--name", "r1"]).exit_code == 0


def test_report_rewrites_all_three_files_without_any_model_or_key(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_scored_run(workdir, monkeypatch)
    for name in ("candidates.html", "candidates.md", "candidates.csv"):
        (workdir / "out" / "r1" / name).unlink()
    for var in ("LLM_API_KEY", "L1_MODEL", "QUERY_MODEL"):
        monkeypatch.delenv(var, raising=False)  # no model settings at all

    def boom(settings: Any) -> None:
        raise AssertionError("report must not build a model client")

    monkeypatch.setattr(cli, "build_llm", boom)
    result = runner.invoke(
        app, ["report", "1", "--db", "data/lit.db", "--out-dir", "out"]
    )
    assert result.exit_code == 0, result.output
    assert "No model was called." in result.output
    assert "2 of 2 papers scored by model-l1, prompt score-v3" in result.output
    assert "Open the list in your browser: out/r1/candidates.html" in result.output
    html = (workdir / "out" / "r1" / "candidates.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "Trust In Automation" in html
    assert (workdir / "out" / "r1" / "candidates.md").exists()
    assert (workdir / "out" / "r1" / "candidates.csv").exists()


def test_report_can_pick_which_models_scores_to_show(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_scored_run(workdir, monkeypatch, model="model-a")
    conn = sqlite3.connect(workdir / "data" / "lit.db")
    with conn:  # a second model scored one paper only, later
        conn.execute(
            "INSERT INTO scores (paper_id, project_hash, input_hash, prompt_version, "
            "model, score, reason, summary, tags) "
            "SELECT paper_id, project_hash, input_hash, prompt_version, 'model-b', "
            "1, 'r', 's', '[]' FROM scores LIMIT 1"
        )
    default = runner.invoke(app, ["report", "1", "--db", "data/lit.db"])
    assert "scored by model-a" in default.output  # the more complete one
    chosen = runner.invoke(
        app, ["report", "1", "--db", "data/lit.db", "--use-model", "model-b"]
    )
    assert "1 of 2 papers scored by model-b" in chosen.output


def test_report_thresholds_and_errors(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_scored_run(workdir, monkeypatch)
    ok = runner.invoke(
        app,
        [
            "report",
            "1",
            "--db",
            "data/lit.db",
            "--min-score",
            "5",
            "--maybe-score",
            "2",
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert "(score 5+)" in (workdir / "out" / "r1" / "candidates.md").read_text(
        encoding="utf-8"
    )
    bad = runner.invoke(
        app,
        [
            "report",
            "1",
            "--db",
            "data/lit.db",
            "--min-score",
            "2",
            "--maybe-score",
            "4",
        ],
    )
    assert (
        bad.exit_code == 2
        and "--min-score and --maybe-score must satisfy" in bad.output
    )
    unknown = runner.invoke(app, ["report", "9", "--db", "data/lit.db"])
    assert unknown.exit_code == 1 and "no run with id 9" in unknown.output
    no_db = runner.invoke(app, ["report", "1", "--db", "data/none.db"])
    assert no_db.exit_code == 1 and "no database at" in no_db.output


def test_the_run_and_score_commands_also_write_the_html(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_scored_run(workdir, monkeypatch)
    assert (workdir / "out" / "r1" / "candidates.html").exists()
    again = runner.invoke(
        app, ["score", "1", "--db", "data/lit.db", "--out-dir", "out"]
    )
    assert "Open the list in your browser:" in again.output
