"""Manual smoke test of the real collectors (a handful of real requests).

    uv run python spikes/smoke_collectors.py ["search phrase"]

Reads .env like the application. Sources run concurrently, each one serially.
Costs about 12 OpenAlex credits and a few Semantic Scholar / arXiv requests.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from litsearch.collectors import build_client
from litsearch.collectors.arxiv import ArxivCollector
from litsearch.collectors.base import CollectorError
from litsearch.collectors.openalex import OpenAlexCollector
from litsearch.collectors.semantic_scholar import SemanticScholarCollector
from litsearch.config import Settings
from litsearch.models import PaperMeta

LIMIT = 5


def show(label: str, papers: list[PaperMeta]) -> list[str]:
    lines = [f"  {label}: {len(papers)} papers"]
    for p in papers[:3]:
        flags = []
        flags.append("abstract" if p.abstract else "no-abstract")
        flags.append(f"doi={p.doi}" if p.doi else "no-doi")
        flags.append(f"arxiv={p.arxiv_id}" if p.arxiv_id else "no-arxiv")
        lines.append(f"    - {p.title[:80]} ({p.year}) [{', '.join(flags)}]")
    return lines


async def exercise(collector, query: str) -> list[str]:
    out = [f"{collector.name}:"]
    try:
        page = await collector.search(query, limit=LIMIT)
        out += show(f"search (total reported: {page.total})", page.items)
        seed = next((p for p in page.items if p.openalex_id or p.s2_id), None)
        if seed is not None and hasattr(collector, "references"):
            out += show("references", await collector.references(seed, 5))
            out += show("citations", await collector.citations(seed, 5))
    except CollectorError as exc:
        out.append(f"  FAILED: status={exc.status.value} ({exc.message})")
    out.append(f"  http requests: {collector.n_requests}")
    if isinstance(collector, OpenAlexCollector):
        out.append(
            f"  OpenAlex credits: used {collector.credits_used}, "
            f"remaining {collector.credits_remaining}/{collector.credits_limit}"
        )
    return out


async def main(query: str) -> None:
    settings = Settings.from_env(env_file=Path(".env"))
    async with build_client(settings) as client:
        collectors = [
            OpenAlexCollector(
                client,
                email=settings.contact_email,
                api_key=settings.openalex_api_key,
            ),
            SemanticScholarCollector(client, api_key=settings.s2_api_key),
            ArxivCollector(client),
        ]
        results = await asyncio.gather(*(exercise(c, query) for c in collectors))
    print(f"query: {query!r}")
    for lines in results:
        print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(
        main(
            sys.argv[1]
            if len(sys.argv) > 1
            else "human-in-the-loop software development agents"
        )
    )
