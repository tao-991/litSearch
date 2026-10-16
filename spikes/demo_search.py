"""Live demo: search the three sources, de-duplicate, print what happened.

    uv run python spikes/demo_search.py ["search phrase"] [per-source limit]

A handful of real requests (about 10 OpenAlex credits). Sources run concurrently, each
one serially. Nothing is stored.
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
from litsearch.pipeline.dedup import deduplicate


async def fetch(collector, query: str, limit: int):  # type: ignore[no-untyped-def]
    try:
        page = await collector.search(query, limit=limit)
        return (
            collector.name,
            page.items,
            f"ok, {len(page.items)} papers (source reports {page.total} matches)",
        )
    except CollectorError as exc:
        return collector.name, [], f"FAILED: {exc.status.value}"


async def main(query: str, limit: int) -> None:
    settings = Settings.from_env(env_file=Path(".env"))
    async with build_client(settings) as client:
        collectors = [
            OpenAlexCollector(
                client, email=settings.contact_email, api_key=settings.openalex_api_key
            ),
            SemanticScholarCollector(client, api_key=settings.s2_api_key),
            ArxivCollector(client),
        ]
        results = await asyncio.gather(*(fetch(c, query, limit) for c in collectors))

    print(f"query: {query!r}, up to {limit} per source\n")
    records: list[PaperMeta] = []
    for name, items, status in results:
        print(f"  {name:9} {status}")
        records += items

    merged = deduplicate(records)
    print(
        f"\n{len(records)} records  ->  {len(merged.papers)} distinct papers "
        f"({len(merged.suspects)} suspected duplicates not merged)\n"
    )
    for i, p in enumerate(merged.papers, 1):
        sources = "+".join(sorted({r.source for r in p.records}))
        print(f"{i:2}. [{p.status:9}] {p.title[:78]}")
        print(
            f"      {p.year or '?'} · {(p.venue or 'venue ?')[:50]} · from {sources}"
            f"{' · DOI ' + p.doi if p.doi else ''}{' · arXiv ' + p.arxiv_id if p.arxiv_id else ''}"
        )
    for s in merged.suspects:
        print(
            f"\nSUSPECT {s.similarity}: {s.paper.title[:60]!r} vs {s.other.title[:60]!r}"
        )


if __name__ == "__main__":
    asyncio.run(
        main(
            sys.argv[1]
            if len(sys.argv) > 1
            else "human-in-the-loop software development agents",
            int(sys.argv[2]) if len(sys.argv) > 2 else 20,
        )
    )
