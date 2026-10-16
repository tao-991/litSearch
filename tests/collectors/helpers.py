"""Test helpers: a fake clock (tests never really wait) and a scripted HTTP server."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FakeTime:
    """Replaces ``asyncio.sleep`` and ``time.monotonic``; records requested sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Server:
    """A scripted stand-in for a data source.

    ``replies`` are consumed in order; each is an ``httpx.Response``, an exception to
    raise, or a callable taking the request and returning one of those. Every request
    seen is kept in ``requests``.
    """

    def __init__(self, replies: Iterable[Any] = ()) -> None:
        self._replies = list(replies)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError(f"unexpected request: {request.url}")
        reply = self._replies.pop(0)
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, httpx.Response)
        return reply

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def json_response(data: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def by_path(routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> Any:
    """Reply according to the URL path (for flows with several different requests)."""

    def reply(request: httpx.Request) -> httpx.Response:
        return routes[request.url.path](request)

    return reply
