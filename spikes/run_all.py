# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Run the Phase 0 spikes: sources concurrently (asyncio.gather), serial within each source.

uv run spikes/run_all.py [openalex] [s2] [arxiv]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import spike_arxiv
import spike_openalex
import spike_s2
from _common import T0, load_env, save_sample

SPIKES = {"openalex": spike_openalex.run, "s2": spike_s2.run, "arxiv": spike_arxiv.run}


async def timed(name, fn, client):
    t = time.monotonic()
    out, log = await fn(client)
    out["_wall_s"] = round(time.monotonic() - t, 1)
    out["_n_http_attempts"] = sum(
        1 for e in log if e.get("status") != "skipped(breaker)"
    )
    return name, out, log


async def main(selected: list[str]) -> None:
    load_env()
    email = os.environ.get("CONTACT_EMAIL")
    ua = "litsearch-spike/0.1" + (f" (mailto:{email})" if email else "")
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers={"User-Agent": ua}
    ) as client:
        results = await asyncio.gather(
            *(timed(n, SPIKES[n], client) for n in selected), return_exceptions=True
        )
    summary: dict = {}
    logs: list = []
    for res in results:
        if isinstance(res, Exception):
            summary.setdefault("_errors", []).append(repr(res))
            continue
        name, out, log = res
        summary[name] = out
        logs.extend(log)
    summary["_total_wall_s"] = round(time.monotonic() - T0, 1)
    save_sample("summary.json", summary)
    save_sample("log.json", sorted(logs, key=lambda e: e.get("t", 0)))
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(SPIKES)))
