"""A small MCP server (stdio) so a chat assistant can use Lit Search.

Made for Claude Desktop.

The chat assistant writes the queries and judges the papers itself; this program only
searches the sources, merges duplicates, stores everything and hands back the papers.
Titles, authors, years, links and DOIs are returned exactly as the data sources gave
them. The protocol part is written by hand (JSON-RPC, one message per line), so no
extra dependency is needed. Nothing but protocol messages may be written to stdout.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import IO, Any

from litsearch import __version__
from litsearch.pipeline import export
from litsearch.profiles import DEFAULT_PROFILE, PROFILES
from litsearch.ui.app import UiApp, UiError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_PAPERS_PER_CALL = 100
ABSTRACT_CHARS = 1200
LOG_TAIL = 12

INSTRUCTIONS = (
    "Lit Search searches OpenAlex, Semantic Scholar and arXiv and merges "
    "duplicates. Write a few focused English queries (2 to 5 for a normal topic) and "
    "show them to the user for approval BEFORE calling start_search. The user's "
    "cheap scoring model reads the abstracts and scores the papers (0 to 5), so do "
    "not read abstracts yourself: call get_papers with min_score (for example 4) "
    "and work from the scores, reasons and summaries it returns; ask for more only "
    "when needed. Poll search_status until it is done. Only report titles, authors, "
    "years, venues, DOIs and links exactly as get_papers returns them; never "
    "invent or complete them."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "start_search",
        "description": (
            "Start a literature search in the background and return a job id. "
            "Takes a few minutes. By default the papers are then scored 0 to 5 by "
            "the user's own cheap model (set in Lit Search), which keeps your own "
            "token use low. Ask the user to approve the queries first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Search phrases, English, each on one sub-question.",
                },
                "profile": {
                    "type": "string",
                    "enum": list(PROFILES),
                    "description": (
                        "Size: test (arXiv only, tiny), quick (default), standard, "
                        "deep. The size caps the number of queries used."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": "Only papers on or after this date, YYYY-MM-DD.",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["openalex", "s2", "arxiv"]},
                },
                "name": {"type": "string", "description": "Optional name of the run."},
                "score": {
                    "type": "boolean",
                    "description": (
                        "Score the papers with the user's cheap model. Default true "
                        "when a scoring model is set up."
                    ),
                },
            },
            "required": ["queries"],
        },
    },
    {
        "name": "search_status",
        "description": "Progress of a search started with start_search.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "get_papers",
        "description": (
            "The papers of a finished run, best first. Scored runs return the score, "
            "the query it fits best, a reason and a short summary written by the "
            "scoring model; bibliographic fields come straight from the data sources."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "At most 100 per call."},
                "offset": {"type": "integer"},
                "min_score": {
                    "type": "integer",
                    "description": "Only papers scored at least this (0 to 5), e.g. 4.",
                },
                "with_abstract": {
                    "type": "boolean",
                    "description": "Default: false for scored runs, true otherwise.",
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "list_runs",
        "description": "Earlier searches, with their queries and paper counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_results",
        "description": (
            "Write the run's ranked list as candidates.html, .md and .csv on the "
            "user's computer and return the folder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "integer"}},
            "required": ["run_id"],
        },
    },
]


class ToolError(Exception):
    """A problem the assistant can read and act on."""


def _int(arguments: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{key} must be a whole number")
    if not low <= value <= high:
        raise ToolError(f"{key} must be between {low} and {high}")
    return value


class McpServer:
    def __init__(self, app: UiApp) -> None:
        self.app = app
        self._tools: dict[str, Callable[[dict[str, Any]], Any]] = {
            "start_search": self._start_search,
            "search_status": self._search_status,
            "get_papers": self._get_papers,
            "list_runs": self._list_runs,
            "save_results": self._save_results,
        }

    # ------------------------------------------------------------------ protocol

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The reply to one JSON-RPC message, or None for a notification."""
        if not isinstance(message, dict) or "method" not in message:
            return self._error(
                message.get("id") if isinstance(message, dict) else None,
                -32600,
                "Invalid request",
            )
        method, msg_id = message["method"], message.get("id")
        if msg_id is None:
            return None  # notifications (such as notifications/initialized)
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return self._result(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "lit-search", "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._result(msg_id, self._call(params))
        return self._error(msg_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, text: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": text},
        }

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        tool = self._tools.get(name) if isinstance(name, str) else None
        try:
            if tool is None:
                raise ToolError(f"Unknown tool: {name}")
            data = tool(arguments)
        except (ToolError, UiError) as exc:
            text = exc.message if isinstance(exc, UiError) else str(exc)
            return {"content": [{"type": "text", "text": text}], "isError": True}
        except Exception as exc:  # noqa: BLE001 - report, never crash the server
            print(f"tool {name} failed: {exc!r}", file=sys.stderr)
            return {
                "content": [{"type": "text", "text": "The tool failed unexpectedly."}],
                "isError": True,
            }
        return {
            "content": [
                {"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}
            ],
            "isError": False,
        }

    # --------------------------------------------------------------------- tools

    def _start_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        settings = self.app.settings()
        ready = bool(settings.llm_api_key and settings.l1_model)
        want = arguments.get("score")
        if want is None:
            want = ready
        if want and not ready:
            raise ToolError(
                "Scoring needs an API key and a scoring model; set them on the "
                "Settings tab of the Lit Search page, or call again with score=false."
            )
        payload: dict[str, Any] = {
            "queries": arguments.get("queries"),
            "profile": arguments.get("profile") or DEFAULT_PROFILE,
            "scoring": {"enabled": bool(want)},
            "origin": "user",
        }
        for key in ("since", "sources", "name"):
            if arguments.get(key):
                payload[key] = arguments[key]
        queries = arguments.get("queries")
        job = self.app.start_run(payload)
        chosen = PROFILES[payload["profile"]]
        used = min(len(queries), chosen.max_queries) if isinstance(queries, list) else 0
        return {
            "job_id": job["id"],
            "run_id": job["run_id"],
            "queries_used": used,
            "scoring": bool(want),
            "note": "Poll search_status with the job_id, then call get_papers.",
        }

    def _search_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str):
            raise ToolError("job_id is required")
        job = self.app.job(job_id)
        out: dict[str, Any] = {
            "status": job["status"],
            "seconds": job["elapsed"],
            "run_id": job["run_id"],
            "recent_log": job["lines"][-LOG_TAIL:],
        }
        if job["status"] == "done":
            out["n_papers"] = job["result"].get("n_papers")
            out["note"] = "Finished. Call get_papers with the run_id."
        elif job["status"] != "running":
            out["error"] = job["error"] or job["status"]
        return out

    def _get_papers(self, arguments: dict[str, Any]) -> dict[str, Any]:
        run_id = _int(arguments, "run_id", 0, 1, 10**9)
        limit = _int(arguments, "limit", 30, 1, MAX_PAPERS_PER_CALL)
        offset = _int(arguments, "offset", 0, 0, 10**6)
        min_score = _int(arguments, "min_score", 0, 0, 5)
        conn = self.app.open_db()
        try:
            candidates = export.load_candidates(
                conn, run_id, self.app.settings().l1_model or None
            )
            summary = export.load_summary(conn, run_id)
        except LookupError:
            raise ToolError(f"No run with id {run_id}") from None
        scored = any(c.score is not None for c in candidates)
        with_abstract = arguments.get("with_abstract", not scored) is not False
        if min_score:
            candidates = [
                c for c in candidates if c.score is not None and c.score >= min_score
            ]
        page = candidates[offset : offset + limit]
        papers = []
        for c in page:
            paper: dict[str, Any] = {
                "id": c.paper_id,
                "title": c.title,
                "authors": list(c.authors),
                "year": c.year,
                "venue": c.venue,
                "status": c.status,
                "doi": c.doi,
                "arxiv_id": c.arxiv_id,
                "links": c.links,
                "open_access_pdf": c.oa_pdf_url,
                "citations": c.citation_count,
                "found_by_queries": list(c.queries),
                "sources": list(c.sources),
            }
            if c.score is not None:
                paper |= {
                    "score": c.score,
                    "fits_query": c.matched_query,
                    "reason": c.reason,
                    "summary": c.summary,
                    "tags": list(c.tags),
                    "scored_from": c.score_basis,
                }
            if with_abstract:
                text = c.abstract or ""
                paper["abstract"] = (
                    text
                    if len(text) <= ABSTRACT_CHARS
                    else text[:ABSTRACT_CHARS] + "..."
                )
            papers.append(paper)
        failed = [f"{s.source}: {s.error}" for s in summary.sources if s.status != "ok"]
        return {
            "run": summary.name,
            "total": len(candidates),
            "scored": scored,
            "offset": offset,
            "returned": len(papers),
            "source_problems": failed,
            "papers": papers,
        }

    def _list_runs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "runs": [
                {k: r[k] for k in ("id", "name", "status", "started_at", "n_papers")}
                | {"queries": r["queries"]}
                for r in self.app.runs()["runs"]
            ]
        }

    def _save_results(self, arguments: dict[str, Any]) -> dict[str, Any]:
        run_id = _int(arguments, "run_id", 0, 1, 10**9)
        return self.app.write_files(run_id, {})


def serve_stdio(
    app: UiApp, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout
) -> None:
    """Read one JSON message per line until the client closes the pipe."""
    server = McpServer(app)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = McpServer._error(None, -32700, "Parse error")
        else:
            reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
