"""Shared helpers for Phase 0 spikes: env loading, a polite serial requester with
retry / backoff / circuit breaker, and request logging.

Throwaway code for measuring external APIs. The production collectors in
litsearch/collectors/ will be written separately.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "spikes" / "samples"  # gitignored
T0 = time.monotonic()

# Same three queries for every source (arXiv gets a hand-converted syntax).
QUERIES: dict[str, str] = {
    "q1": "developer trust acceptance of AI-generated code changes",
    "q2": "coding agent pull request review acceptance",
    "q3": "LLM explaining code changes to support code review",
}
N_PER_QUERY = 20


def load_env() -> None:
    """Minimal .env loader. Never prints values."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v:
            os.environ.setdefault(k.strip(), v)


def save_sample(name: str, data: Any) -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    path = SAMPLES / name
    if isinstance(data, (dict, list)):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        path.write_text(str(data))


def _rate_headers(resp: httpx.Response) -> dict[str, str]:
    keep = ("ratelimit", "rate-limit", "retry-after", "x-rate")
    return {k: v for k, v in resp.headers.items() if any(s in k.lower() for s in keep)}


class Source:
    """Serial requester for one data source.

    - min_interval: minimum seconds between request starts (per source)
    - retries on 429 / 5xx / network errors with Retry-After or exponential backoff
    - circuit breaker: opens after `breaker_limit` consecutive give-ups
    Secrets (mailto, api keys) go in `secret_params` / `headers` and are never logged.
    """

    def __init__(
        self,
        name: str,
        client: httpx.AsyncClient,
        min_interval: float,
        max_retries: int = 3,
        breaker_limit: int = 3,
    ) -> None:
        self.name = name
        self.client = client
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.breaker_limit = breaker_limit
        self.log: list[dict[str, Any]] = []
        self._last_start = 0.0
        self._consec_fail = 0
        self.breaker_open = False

    async def get(
        self,
        url: str,
        label: str,
        params: dict[str, Any] | None = None,
        secret_params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry: bool = True,
    ) -> httpx.Response | None:
        if self.breaker_open:
            self.log.append(
                {"source": self.name, "label": label, "status": "skipped(breaker)"}
            )
            return None
        attempt = 0
        while True:
            gap = self.min_interval - (time.monotonic() - self._last_start)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_start = time.monotonic()
            status: int | None = None
            err: str | None = None
            resp: httpx.Response | None = None
            try:
                resp = await self.client.get(
                    url,
                    params={**(params or {}), **(secret_params or {})},
                    headers=headers,
                )
                status = resp.status_code
            except httpx.HTTPError as e:
                err = type(e).__name__
            elapsed = time.monotonic() - self._last_start
            entry: dict[str, Any] = {
                "source": self.name,
                "label": label,
                "attempt": attempt,
                "t": round(self._last_start - T0, 2),
                "status": status,
                "elapsed_s": round(elapsed, 2),
            }
            if err:
                entry["error"] = err
            if resp is not None:
                rh = _rate_headers(resp)
                if rh:
                    entry["rate_headers"] = rh
            retriable = status is None or status == 429 or status >= 500
            if not retriable:
                self._consec_fail = 0
                self.log.append(entry)
                return resp
            # retriable failure
            if retry and attempt < self.max_retries:
                wait = 2 ** (attempt + 1)  # 2, 4, 8
                if resp is not None:
                    ra = resp.headers.get("retry-after")
                    if ra and ra.isdigit():
                        wait = min(int(ra), 60)
                entry["backoff_s"] = wait
                self.log.append(entry)
                await asyncio.sleep(wait)
                attempt += 1
                continue
            self.log.append(entry)
            self._consec_fail += 1
            if self._consec_fail >= self.breaker_limit:
                self.breaker_open = True
            return None


def has_text(s: Any) -> bool:
    return isinstance(s, str) and bool(s.strip())


def pct(n: int, total: int) -> str:
    return f"{n}/{total}" + (f" ({100 * n // total}%)" if total else "")
