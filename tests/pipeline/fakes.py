"""Stand-ins for collectors and the clock, shared by pipeline and CLI tests."""

from __future__ import annotations

import itertools
from datetime import date
from typing import Any

from litsearch.models import Page, PaperMeta

_ids = itertools.count(1)


def meta(title: str, **kwargs: Any) -> PaperMeta:
    """A record with sensible defaults; every call gets a fresh source ID."""
    fields: dict[str, Any] = {
        "source": "openalex",
        "source_id": f"X{next(_ids)}",
        "title": title,
        "authors": ("Jing Jiang", "Ana Costa"),
        "year": 2025,
    }
    fields.update(kwargs)
    return PaperMeta(**fields)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeCollector:
    """Answers ``search`` from a script: papers, or an exception, per query."""

    def __init__(
        self,
        name: str,
        script: dict[str, Any],
        clock: FakeClock | None = None,
        seconds_per_call: float = 0.0,
    ) -> None:
        self.name = name
        self.script = script
        self.n_requests = 0
        self.calls: list[dict[str, Any]] = []
        self._clock = clock
        self._seconds = seconds_per_call

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        since: date | None = None,
        mode: str | None = None,
    ) -> Page:
        self.n_requests += 1
        self.calls.append(
            {"query": query, "limit": limit, "since": since, "mode": mode}
        )
        if self._clock is not None:
            self._clock.now += self._seconds
        reply = self.script.get(query, [])
        if isinstance(reply, Exception):
            raise reply
        return Page(items=list(reply), total=len(reply))
