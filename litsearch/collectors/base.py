"""Shared behaviour of all collectors: politeness, retries, failure classification.

Every collector talks to one data source through ``HttpCollector._get``, which

- serializes requests (concurrency 1 per source) and keeps a minimum interval between
  them,
- retries rate limits (429), server errors (5xx) and network errors with backoff,
  honouring ``Retry-After``,
- opens a circuit breaker after repeated failures so a dead source is not hammered,
- turns every final failure into a ``CollectorError`` carrying a ``SourceStatus`` that
  maps one-to-one onto ``source_runs.status``.

Secrets (API keys, contact e-mail) are passed separately from ordinary parameters and
never appear in logs or error messages.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

import httpx

from litsearch.models import Page, PaperMeta

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 60.0


class SourceStatus(StrEnum):
    """Outcome of using a source; values match ``source_runs.status``."""

    OK = "ok"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"
    SKIPPED = "skipped"


class CollectorError(Exception):
    """A source could not be used. ``status`` says how, for ``source_runs``."""

    def __init__(self, status: SourceStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Collector(Protocol):
    """What the pipeline needs from every data source (SPEC §4)."""

    name: str

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        since: date | None = None,
    ) -> Page: ...


class CitationCollector(Collector, Protocol):
    """Sources that can also follow citations (used for snowballing)."""

    async def references(self, paper: PaperMeta, limit: int) -> list[PaperMeta]: ...

    async def citations(self, paper: PaperMeta, limit: int) -> list[PaperMeta]: ...


class HttpCollector:
    """Base class holding the polite-HTTP machinery. Subclasses set ``name``."""

    name: str = "source"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        min_interval: float,
        max_retries: int = 3,
        breaker_limit: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._breaker_limit = breaker_limit
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_start: float | None = None
        self._consecutive_failures = 0
        self.breaker_open = False
        self.n_requests = 0
        self.last_error: str | None = None

    async def _get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        secret_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        ok_statuses: Collection[int] = (200,),
    ) -> httpx.Response:
        """GET with politeness and retries; raise ``CollectorError`` on final failure.

        Responses whose status is in ``ok_statuses`` are returned to the caller
        (e.g. pass ``(200, 404)`` when "not found" is an expected answer).
        """
        async with self._lock:
            if self.breaker_open:
                raise CollectorError(
                    SourceStatus.SKIPPED,
                    f"{self.name}: skipped after {self._breaker_limit} consecutive "
                    "failures",
                )
            return await self._get_with_retries(
                url, params, secret_params, headers, ok_statuses
            )

    async def _get_with_retries(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        secret_params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        ok_statuses: Collection[int],
    ) -> httpx.Response:
        full_params = {**(params or {}), **(secret_params or {})}
        attempt = 0
        while True:
            await self._wait_for_turn()
            self.n_requests += 1
            response: httpx.Response | None = None
            status: int | None = None
            problem: str
            try:
                response = await self._client.get(
                    url, params=full_params, headers=headers
                )
                status = response.status_code
                problem = f"HTTP {status}"
                self._observe(response)
            except httpx.HTTPError as exc:
                # Only the exception type: its text may contain the request URL.
                problem = type(exc).__name__

            if status is not None and status in ok_statuses:
                self._consecutive_failures = 0
                self.last_error = None
                assert response is not None
                return response

            retriable = status is None or status == 429 or status >= 500
            if not retriable:
                self.last_error = problem
                kind = (
                    SourceStatus.BLOCKED if status in (401, 403) else SourceStatus.ERROR
                )
                raise CollectorError(kind, f"{self.name}: {problem}")

            if attempt < self._max_retries:
                delay = self._backoff(attempt, response)
                logger.warning(
                    "%s: %s (attempt %d); retrying in %.0fs",
                    self.name,
                    problem,
                    attempt + 1,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1
                continue

            self.last_error = problem
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._breaker_limit:
                self.breaker_open = True
            kind = SourceStatus.RATE_LIMITED if status == 429 else SourceStatus.ERROR
            raise CollectorError(kind, f"{self.name}: {problem} after retries")

    def _observe(self, response: httpx.Response) -> None:
        """Hook: look at every response received (e.g. for quota headers)."""

    async def _wait_for_turn(self) -> None:
        if self._last_start is not None:
            gap = self._min_interval - (self._clock() - self._last_start)
            if gap > 0:
                await self._sleep(gap)
        self._last_start = self._clock()

    @staticmethod
    def _backoff(attempt: int, response: httpx.Response | None) -> float:
        delay = float(2 ** (attempt + 1))  # 2, 4, 8 seconds
        if response is not None:
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        return delay


def build_user_agent(version: str, contact_email: str | None) -> str:
    agent = f"litsearch/{version}"
    return f"{agent} (mailto:{contact_email})" if contact_email else agent
