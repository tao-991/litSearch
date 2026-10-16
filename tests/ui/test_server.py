"""The web interface end to end: a real local server, fake collectors and model."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import httpx
import pytest

from litsearch.ui.app import UiApp
from litsearch.ui.server import UiServer, make_server
from tests.llm.fakes import FakeLLM
from tests.pdf_fixtures import make_pdf
from tests.pipeline.fakes import FakeCollector, meta

KEY = "sk-secret-value-123"
QUERY = "agent code review acceptance"


class Env:
    def __init__(self, server: UiServer, app: UiApp, home: Path, llm: FakeLLM) -> None:
        self.server, self.app, self.home, self.llm = server, app, home, llm
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.opened: list[Path] = []

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, Any]:
        head = {"X-Token": self.server.token if token is None else token}
        head.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            head["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, method=method, headers=head)
        try:
            with urlopen(request) as reply:
                text = reply.read().decode()
                status = reply.status
        except HTTPError as err:
            text, status = err.read().decode(), err.code
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def wait_job(self, job_id: str) -> dict[str, Any]:
        for _ in range(200):
            _, job = self.call("GET", f"/api/jobs/{job_id}")
            if job["status"] != "running":
                return job  # type: ignore[no-any-return]
            time.sleep(0.05)
        raise AssertionError("job did not finish")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    (tmp_path / ".env").write_text(
        f"LLM_API_KEY={KEY}\nL1_MODEL=m-score\nQUERY_MODEL=m-query\n"
    )
    llm = FakeLLM(["# Project\nA brief."])
    collector = FakeCollector(
        "openalex", {QUERY: [meta("A study of agent code review acceptance")]}
    )
    opened: list[Path] = []
    app = UiApp(
        tmp_path,
        collectors_factory=lambda client, settings, sources: {"openalex": collector},
        llm_factory=lambda settings: llm,
        client_factory=lambda settings: httpx.AsyncClient(),
        folder_opener=opened.append,
    )
    server = make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    e = Env(server, app, tmp_path, llm)
    e.opened = opened
    yield e
    server.shutdown()
    server.server_close()


def test_page_is_served_with_a_token_and_a_csp(env: Env) -> None:
    with urlopen(env.base + "/") as reply:
        page = reply.read().decode()
        csp = reply.headers["Content-Security-Policy"]
    assert env.server.token in page
    assert "__TOKEN__" not in page
    assert "default-src 'none'" in csp


def test_api_needs_the_token(env: Env) -> None:
    assert env.call("GET", "/api/state", token="wrong")[0] == 403
    assert env.call("POST", "/api/heartbeat", {}, token="")[0] == 403
    assert env.call("GET", "/api/state")[0] == 200


def test_foreign_host_and_origin_are_refused(env: Env) -> None:
    assert env.call("GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
    status, _ = env.call(
        "POST", "/api/heartbeat", {}, headers={"Origin": "http://evil.example"}
    )
    assert status == 403


def test_state_never_contains_the_key(env: Env) -> None:
    _, state = env.call("GET", "/api/state")
    assert KEY not in json.dumps(state)
    assert state["settings"]["LLM_API_KEY"] == {"set": True}
    assert state["ready"] == {"models": True, "scoring": True}


def test_save_settings_keeps_the_key_unless_replaced(env: Env) -> None:
    status, state = env.call(
        "POST",
        "/api/settings",
        {"set": {"L1_MODEL": "other", "LLM_API_KEY": "", "CONTACT_EMAIL": "a@b.co"}},
    )
    assert status == 200
    text = (env.home / ".env").read_text()
    assert f"LLM_API_KEY={KEY}" in text.replace('"', "")
    assert state["settings"]["L1_MODEL"]["value"] == "other"
    assert KEY not in json.dumps(state)
    env.call("POST", "/api/settings", {"set": {}, "clear": ["LLM_API_KEY"]})
    _, state = env.call("GET", "/api/state")
    assert state["settings"]["LLM_API_KEY"] == {"set": False}


def test_unknown_setting_is_refused(env: Env) -> None:
    status, reply = env.call("POST", "/api/settings", {"set": {"DATA_DIR": "/etc"}})
    assert status == 400
    assert "cannot be changed" in reply["error"]


def test_brief_and_upload(env: Env) -> None:
    status, up = env.call(
        "POST",
        "/api/upload-pdf",
        headers={"X-Filename": "my%20paper.pdf", "Content-Type": "application/pdf"},
        raw=make_pdf([["Hello agents and code review"]]),
    )
    assert status == 200 and up["name"] == "my paper.pdf"
    status, brief = env.call(
        "POST", "/api/brief", {"about": "agents", "pdf_ids": [up["id"]]}
    )
    assert status == 200 and "brief" in brief["brief"].lower()
    assert "my paper.pdf" in env.llm.calls[0]["user"]


def test_upload_rejects_non_pdf(env: Env) -> None:
    status, reply = env.call("POST", "/api/upload-pdf", raw=b"not a pdf at all")
    assert status == 400 and reply["error"]
    assert not list((env.home / "uploads").glob("*.pdf"))


def test_brief_without_input_is_an_error(env: Env) -> None:
    assert env.call("POST", "/api/brief", {"about": "  "})[0] == 400


def test_estimate_and_run_end_to_end(env: Env) -> None:
    payload = {
        "queries": [QUERY, QUERY],
        "sources": ["openalex"],
        "profile": "quick",
        "scoring": {"enabled": False},
    }
    status, est = env.call("POST", "/api/estimate", payload)
    assert status == 200 and est["requests"]
    status, job = env.call("POST", "/api/runs/start", payload)
    assert status == 200
    done = env.wait_job(job["id"])
    assert done["status"] == "done", done
    run_id = done["result"]["run_id"]
    assert done["result"]["n_papers"] == 1
    assert (env.home / "out" / done["result"]["name"] / "candidates.html").is_file()

    _, runs = env.call("GET", "/api/runs")
    assert [r["id"] for r in runs["runs"]] == [run_id]

    token = env.server.token
    status, page = env.call(
        "GET", f"/runs/{run_id}/report.html?t={token}&min=4&maybe=3", token=""
    )
    assert status == 200 and "A study of agent code review" in page
    assert env.call("GET", f"/runs/{run_id}/report.html", token="")[0] == 403
    assert env.call("GET", f"/runs/999/report.html?t={token}", token="")[0] == 404
    status, _ = env.call(
        "GET", f"/runs/{run_id}/report.html?t={token}&min=2&maybe=4", token=""
    )
    assert status == 400

    status, _ = env.call("POST", f"/api/runs/{run_id}/files", {"min": 4, "maybe": 3})
    assert status == 200
    assert env.call("POST", "/api/open-folder", {"run_id": run_id})[0] == 200
    assert env.opened and env.opened[0].name == done["result"]["name"]


def test_second_job_is_refused_while_one_runs(env: Env) -> None:
    release = threading.Event()

    class Slow(FakeCollector):
        async def search(self, *args: Any, **kwargs: Any) -> Any:
            while not release.is_set():
                import asyncio

                await asyncio.sleep(0.02)
            return await super().search(*args, **kwargs)

    env.app.collectors_factory = lambda c, s, src: {"openalex": Slow("openalex", {})}
    payload = {
        "queries": ["x y z"],
        "sources": ["openalex"],
        "scoring": {"enabled": False},
    }
    _, job = env.call("POST", "/api/runs/start", payload)
    status, _ = env.call("POST", "/api/runs/start", payload)
    assert status == 409
    _, state = env.call("GET", "/api/state")
    assert state["job"]["id"] == job["id"]
    env.call("POST", f"/api/jobs/{job['id']}/cancel", {})
    done = env.wait_job(job["id"])
    assert done["status"] == "cancelled"
    release.set()


def test_bad_run_requests(env: Env) -> None:
    assert env.call("POST", "/api/runs/start", {"queries": []})[0] == 400
    bad = {"queries": ["a b"], "sources": ["nope"]}
    assert env.call("POST", "/api/runs/start", bad)[0] == 400
    assert (
        env.call("POST", "/api/runs/start", {"queries": ["a b"], "since": "x"})[0]
        == 400
    )


def test_unknown_paths_and_bad_json(env: Env) -> None:
    assert env.call("GET", "/api/nothing")[0] == 404
    assert env.call("POST", "/api/settings", raw=b"{oops")[0] == 400


def test_idle_and_quit(env: Env) -> None:
    now = [0.0]
    env.app.clock = lambda: now[0]
    env.app.last_beat = 0.0
    now[0] = 100.0
    assert env.app.idle_seconds() == 100.0
    env.call("POST", "/api/heartbeat", {})
    assert env.app.idle_seconds() == 0.0
    env.call("POST", "/api/quit", {})
    assert env.app.quit_requested
