from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from litsearch.mcp_server import TOOLS, McpServer, serve_stdio
from litsearch.ui.app import UiApp
from tests.pipeline.fakes import FakeCollector, meta

QUERY = "agent code review acceptance"


class NoModel:
    """The assistant judges the papers; any model call here is a bug."""

    def __call__(self, settings: Any) -> Any:
        raise AssertionError("the MCP search must not call a model")


@pytest.fixture
def server(tmp_path: Path) -> McpServer:
    (tmp_path / ".env").write_text("CONTACT_EMAIL=a@b.co\n")
    collector = FakeCollector(
        "arxiv",
        {QUERY: [meta("A study of agent code review", source="arxiv", year=2025)]},
    )
    app = UiApp(
        tmp_path,
        collectors_factory=lambda client, settings, sources: {"arxiv": collector},
        llm_factory=NoModel(),
        client_factory=lambda settings: httpx.AsyncClient(),
        folder_opener=lambda path: None,
    )
    return McpServer(app)


def rpc(server: McpServer, method: str, params: Any = None, msg_id: int = 1) -> Any:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def call(server: McpServer, name: str, **arguments: Any) -> tuple[bool, Any]:
    reply = rpc(server, "tools/call", {"name": name, "arguments": arguments})
    result = reply["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] else json.loads(text))


def test_initialize_and_tool_list(server: McpServer) -> None:
    reply = rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in reply["result"]["capabilities"]
    odd = rpc(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert odd["result"]["protocolVersion"] == "2025-06-18"
    names = [t["name"] for t in rpc(server, "tools/list")["result"]["tools"]]
    assert names == [t["name"] for t in TOOLS]
    assert "start_search" in names and "get_papers" in names


def test_notifications_get_no_reply_and_unknown_methods_fail(server: McpServer) -> None:
    assert (
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    )
    assert rpc(server, "nope")["error"]["code"] == -32601
    assert rpc(server, "ping")["result"] == {}


def test_search_then_read_papers(server: McpServer) -> None:
    error, started = call(server, "start_search", queries=[QUERY], profile="test")
    assert not error and started["job_id"]
    for _ in range(200):
        _, status = call(server, "search_status", job_id=started["job_id"])
        if status["status"] != "running":
            break
        time.sleep(0.05)
    assert status["status"] == "done", status
    assert status["n_papers"] == 1
    error, papers = call(server, "get_papers", run_id=started["run_id"])
    assert not error and papers["total"] == 1
    first = papers["papers"][0]
    assert first["title"] == "A study of agent code review"
    assert first["found_by_queries"] == [QUERY]
    _, bare = call(server, "get_papers", run_id=started["run_id"], with_abstract=False)
    assert "abstract" not in bare["papers"][0]
    _, runs = call(server, "list_runs")
    assert runs["runs"][0]["id"] == started["run_id"]
    _, saved = call(server, "save_results", run_id=started["run_id"])
    assert (Path(saved["folder"]) / "candidates.html").is_file()


def test_the_profile_caps_the_queries(server: McpServer) -> None:
    _, started = call(
        server, "start_search", queries=["a b", "c d", "e f"], profile="test"
    )
    assert started["queries_used"] == 2


def test_errors_are_readable_not_crashes(server: McpServer) -> None:
    error, text = call(server, "start_search", queries=[])
    assert error and "query" in text
    error, text = call(server, "get_papers", run_id=999)
    assert error and "999" in text
    error, text = call(server, "get_papers", run_id="x")
    assert error and "whole number" in text
    error, text = call(server, "nothing")
    assert error and "Unknown tool" in text


def test_stdio_loop_writes_only_protocol_lines(server: McpServer) -> None:
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        "",
        "this is not json",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    out = io.StringIO()
    serve_stdio(server.app, io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700
