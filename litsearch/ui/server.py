"""The HTTP side of the web interface: a tiny server on this computer only.

Standard library only. Safeguards, because the page can start searches and edit the
settings: it listens on 127.0.0.1, answers only requests whose ``Host`` is the local
address (against DNS rebinding), refuses requests from other web sites (``Origin``),
needs a random token that only the served page knows, and never returns API keys.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from litsearch.ui.app import MAX_UPLOAD_BYTES, UiApp, UiError

DEFAULT_PORT = 8765
IDLE_LIMIT_SECONDS = 150.0  # a hidden browser tab may only ping once a minute
MAX_JSON_BYTES = 1_000_000

PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'"
)


def _index_html() -> str:
    return (
        resources.files("litsearch.ui")
        .joinpath("index.html")
        .read_text(encoding="utf-8")
    )


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: UiApp, token: str) -> None:
        super().__init__(address, Handler)
        self.app = app
        self.token = token

    @property
    def allowed_hosts(self) -> set[str]:
        port = self.server_port
        return {f"127.0.0.1:{port}", f"localhost:{port}"}


class Handler(BaseHTTPRequestHandler):
    server: UiServer
    protocol_version = "HTTP/1.1"
    _body_read = False

    def log_message(self, format: str, *args: Any) -> None:
        return  # keep the terminal quiet

    # ------------------------------------------------------------------ checks

    def _guard(self, *, needs_token: bool) -> bool:
        host = self.headers.get("Host", "").lower()
        if host not in self.server.allowed_hosts:
            self._json({"error": "forbidden host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {
            f"http://{h}" for h in self.server.allowed_hosts
        }:
            self._json({"error": "forbidden origin"}, 403)
            return False
        if needs_token:
            given = (
                self.headers.get("X-Token")
                or parse_qs(urlparse(self.path).query).get("t", [""])[0]
            )
            if not hmac.compare_digest(given.encode(), self.server.token.encode()):
                self._json({"error": "missing or wrong token"}, 403)
                return False
        return True

    # --------------------------------------------------------------- responses

    def _send(
        self, body: bytes, status: int, content_type: str, *, page: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if page:
            self.send_header("Content-Security-Policy", PAGE_CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            status,
            "application/json; charset=utf-8",
        )

    def _error(self, exc: UiError) -> None:
        body: dict[str, Any] = {"error": exc.message}
        if exc.kind:
            body["kind"] = exc.kind
        self._json(body, exc.status)

    def _body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise UiError("bad Content-Length") from None
        if length < 0 or length > limit:
            self.close_connection = (
                True  # the unread body would corrupt the next request
            )
            raise UiError("The request is too large.", 413)
        self._body_read = True
        return self.rfile.read(length)

    def _payload(self) -> dict[str, Any]:
        raw = self._body(MAX_JSON_BYTES)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise UiError("The request is not valid JSON.") from None
        if not isinstance(data, dict):
            raise UiError("The request must be a JSON object.")
        return data

    # ------------------------------------------------------------------ routing

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            if not self._guard(needs_token=False):
                return
            page = _index_html().replace("__TOKEN__", self.server.token)
            self._send(page.encode("utf-8"), 200, "text/html; charset=utf-8", page=True)
            return
        if not self._guard(needs_token=True):
            return
        try:
            self._get(path)
        except UiError as exc:
            self._error(exc)

    def _get(self, path: str) -> None:
        app = self.server.app
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if path == "/api/state":
            self._json(app.state())
        elif path == "/api/runs":
            self._json(app.runs())
        elif path == "/api/models":
            self._json(app.models())
        elif match := re.fullmatch(r"/api/jobs/([0-9a-f]+)", path):
            since = int(query["since"]) if query.get("since", "").isdigit() else 0
            self._json(app.job(match.group(1), since))
        elif match := re.fullmatch(r"/runs/(\d+)/report\.html", path):
            page = app.report_html(int(match.group(1)), query)
            self._send(page.encode("utf-8"), 200, "text/html; charset=utf-8", page=True)
        else:
            raise UiError("Not found.", 404)

    def do_POST(self) -> None:
        self._body_read = False
        if not self._guard(needs_token=True):
            self.close_connection = True  # do not read the body of a refused request
            return
        path = urlparse(self.path).path
        try:
            self._post(path)
        except UiError as exc:
            self._error(exc)
        except Exception:
            self._json({"error": "Something went wrong inside the program."}, 500)
            raise

    def _post(self, path: str) -> None:
        try:
            self._route_post(path)
        finally:
            if not self._body_read and not self.close_connection:
                try:
                    self._body(MAX_JSON_BYTES)  # an unread body would break keep-alive
                except UiError:
                    pass

    def _route_post(self, path: str) -> None:
        app = self.server.app
        simple: dict[str, Callable[[dict[str, Any]], Any]] = {
            "/api/settings": app.save_settings,
            "/api/brief": app.brief,
            "/api/queries": app.queries,
            "/api/estimate": app.estimate,
            "/api/runs/start": app.start_run,
        }
        if path in simple:
            self._json(simple[path](self._payload()))
        elif path == "/api/check-llm":
            self._json(app.check_llm())
        elif path == "/api/heartbeat":
            self._json(app.heartbeat())
        elif path == "/api/quit":
            self._json(app.request_quit())
        elif path == "/api/open-folder":
            run_id = self._payload().get("run_id")
            self._json(
                app.open_folder(int(run_id) if isinstance(run_id, int) else None)
            )
        elif path == "/api/upload-pdf":
            name = unquote(self.headers.get("X-Filename", "paper.pdf"))
            self._json(app.upload_pdf(name, self._body(MAX_UPLOAD_BYTES + 1024)))
        elif match := re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", path):
            self._json(app.cancel(match.group(1)))
        elif match := re.fullmatch(r"/api/runs/(\d+)/score", path):
            self._json(app.start_score(int(match.group(1)), self._payload()))
        elif match := re.fullmatch(r"/api/runs/(\d+)/files", path):
            self._json(app.write_files(int(match.group(1)), self._payload()))
        else:
            raise UiError("Not found.", 404)


def make_server(app: UiApp, *, port: int = DEFAULT_PORT) -> UiServer:
    """A server for ``app`` on the preferred port, or any free one if it is taken."""
    token = secrets.token_urlsafe(24)
    try:
        return UiServer(("127.0.0.1", port), app, token)
    except OSError:
        return UiServer(("127.0.0.1", 0), app, token)


def serve(
    app: UiApp,
    *,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    auto_exit: bool = True,
    announce: Callable[[str], None] = print,
) -> None:
    """Run until the page is closed (``auto_exit``), Quit is pressed, or Ctrl+C."""
    server = make_server(app, port=port)
    url = f"http://127.0.0.1:{server.server_port}/"
    announce(f"Lit Search is running at {url}")
    announce(
        "It stops by itself a few minutes after you close the page "
        "(or press Quit on the page, or Ctrl+C here)."
    )

    def watch() -> None:
        while True:
            time.sleep(1)
            if app.quit_requested or (
                auto_exit and app.idle_seconds() > IDLE_LIMIT_SECONDS
            ):
                server.shutdown()
                return

    threading.Thread(target=watch, name="idle-watch", daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
