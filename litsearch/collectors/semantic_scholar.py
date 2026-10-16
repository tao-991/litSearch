"""Semantic Scholar (Graph API) collector: search, references, citations.

Phase 0 findings (docs/spike-findings.md): unauthenticated requests share a quota with
every anonymous user and most of them got HTTP 429, so this source is best-effort. A
429 surfaces as ``CollectorError(RATE_LIMITED)`` and must never fail a whole run.
Relevance search allows ``offset + limit < 1000`` and ``limit <= 100``.

The references / citations response shape follows the API documentation; it could not
be verified live in Phase 0.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx

from litsearch.collectors.base import HttpCollector
from litsearch.models import Page, PaperMeta
from litsearch.normalize import normalize_arxiv_id, normalize_doi

BASE_URL = "https://api.semanticscholar.org/graph/v1"
MAX_PER_PAGE = 100
MAX_WINDOW = 999  # offset + limit must stay below 1000
FIELDS = ",".join(
    [
        "title",
        "abstract",
        "authors",
        "year",
        "venue",
        "publicationDate",
        "externalIds",
        "openAccessPdf",
        "citationCount",
        "url",
    ]
)


def parse_paper(data: Mapping[str, Any] | None) -> PaperMeta | None:
    """Convert one Semantic Scholar paper (``None`` if unmatched or untitled)."""
    if not data:
        return None
    paper_id = data.get("paperId")
    title = (data.get("title") or "").strip()
    if not paper_id or not title:
        return None
    external = data.get("externalIds") or {}
    doi = normalize_doi(external.get("DOI"))
    arxiv_id = normalize_arxiv_id(external.get("ArXiv"))

    # Links come from identifiers the source returned.
    links: dict[str, str] = {}
    if data.get("url"):
        links["semantic_scholar"] = data["url"]
    if doi:
        links["doi"] = f"https://doi.org/{doi}"
    if arxiv_id:
        links["arxiv"] = f"https://arxiv.org/abs/{arxiv_id}"

    authors = tuple(
        name for a in data.get("authors") or [] if (name := (a or {}).get("name"))
    )
    return PaperMeta(
        source="s2",
        source_id=paper_id,
        title=title,
        abstract=(data.get("abstract") or "").strip() or None,
        authors=authors,
        year=data.get("year"),
        date=data.get("publicationDate"),
        venue=(data.get("venue") or "").strip() or None,
        doi=doi,
        arxiv_id=arxiv_id,
        s2_id=paper_id,
        citation_count=data.get("citationCount"),
        links=links,
        oa_pdf_url=(data.get("openAccessPdf") or {}).get("url"),
        raw=dict(data),
    )


class SemanticScholarCollector(HttpCollector):
    name = "s2"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        min_interval: float = 1.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, min_interval=min_interval, **kwargs)
        self._api_key = api_key

    def _headers(self) -> dict[str, str] | None:
        return {"x-api-key": self._api_key} if self._api_key else None

    async def _json(
        self,
        path: str,
        params: Mapping[str, Any],
        ok_statuses: tuple[int, ...] = (200,),
    ) -> tuple[int, dict[str, Any]]:
        response = await self._get(
            f"{BASE_URL}{path}",
            params=params,
            headers=self._headers(),
            ok_statuses=ok_statuses,
        )
        return response.status_code, response.json()

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        since: date | None = None,
    ) -> Page:
        """One page of relevance-ranked results (at most 100, within the first 999)."""
        if not query.strip():
            raise ValueError("query must not be empty")
        if limit < 1 or offset < 0:
            raise ValueError("limit must be >= 1 and offset >= 0")
        limit = min(limit, MAX_PER_PAGE, MAX_WINDOW - offset)
        if limit < 1:
            return Page(items=[], total=None, next_offset=None)
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "fields": FIELDS,
        }
        if since is not None:
            params["year"] = f"{since.year}-"  # whole years only
        _, body = await self._json("/paper/search", params)
        data = body.get("data") or []
        next_offset = body.get("next")
        if next_offset is not None and next_offset > MAX_WINDOW:
            next_offset = None
        return Page(
            items=[p for d in data if (p := parse_paper(d)) is not None],
            total=body.get("total"),
            next_offset=next_offset,
        )

    @staticmethod
    def _paper_ref(paper: PaperMeta) -> str | None:
        if paper.s2_id:
            return paper.s2_id
        if paper.doi:
            return f"DOI:{paper.doi}"
        if paper.arxiv_id:
            return f"ARXIV:{paper.arxiv_id}"
        return None

    async def _related(
        self, paper: PaperMeta, limit: int, kind: str, key: str
    ) -> list[PaperMeta]:
        ref = self._paper_ref(paper)
        if ref is None or limit < 1:
            return []
        found: list[PaperMeta] = []
        offset = 0
        while len(found) < limit:
            status, body = await self._json(
                f"/paper/{ref}/{kind}",
                {
                    "fields": FIELDS,
                    "limit": min(limit - len(found), MAX_PER_PAGE),
                    "offset": offset,
                },
                ok_statuses=(200, 404),
            )
            if status == 404:
                return []
            for entry in body.get("data") or []:
                parsed = parse_paper((entry or {}).get(key))
                if parsed is not None:  # unmatched references have no paperId
                    found.append(parsed)
            offset = body.get("next")
            if offset is None:
                break
        return found[:limit]

    async def references(self, paper: PaperMeta, limit: int) -> list[PaperMeta]:
        return await self._related(paper, limit, "references", "citedPaper")

    async def citations(self, paper: PaperMeta, limit: int) -> list[PaperMeta]:
        return await self._related(paper, limit, "citations", "citingPaper")
