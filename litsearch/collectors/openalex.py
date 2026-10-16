"""OpenAlex collector (SPEC §4): search, references, citations, quota tracking.

Behaviour verified in Phase 0 (docs/spike-findings.md):
- Without an API key every network egress IP shares 1000 credits per day; a request
  with a search term costs 10 credits, a pure filter request 1, a single-entity
  lookup 0.
- ``page`` paging reaches at most 10,000 results; ``cursor`` paging needs a key.
- ``abstract_inverted_index`` must be turned back into text.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx

from litsearch.collectors.base import HttpCollector
from litsearch.models import Page, PaperMeta
from litsearch.normalize import (
    arxiv_id_from_doi,
    normalize_arxiv_id,
    normalize_doi,
    openalex_short_id,
)

BASE_URL = "https://api.openalex.org"
SEARCH_CREDITS = 10  # request with a search term
FILTER_CREDITS = 1  # request with only filters
MAX_PAGEABLE = 10_000  # `page` paging stops here without an API key
MAX_PER_PAGE = 200
ID_BATCH = 50  # works fetched per `filter=openalex:W1|W2|...` request

SELECT = ",".join(
    [
        "id",
        "doi",
        "title",
        "display_name",
        "publication_year",
        "publication_date",
        "type",
        "authorships",
        "primary_location",
        "locations",
        "best_oa_location",
        "open_access",
        "cited_by_count",
        "abstract_inverted_index",
    ]
)


def estimate_credits(n_search_requests: int, n_filter_requests: int = 0) -> int:
    """Credits needed for the given number of requests (for ``--dry-run``)."""
    return n_search_requests * SEARCH_CREDITS + n_filter_requests * FILTER_CREDITS


def reconstruct_abstract(inverted: Mapping[str, list[int]] | None) -> str | None:
    """Rebuild abstract text from OpenAlex's word -> positions index."""
    if not inverted:
        return None
    by_position: dict[int, str] = {}
    for word, positions in inverted.items():
        for position in positions:
            by_position[position] = word
    text = " ".join(by_position[i] for i in sorted(by_position))
    return text or None


def _locations(work: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    primary = work.get("primary_location")
    found = [primary] if primary else []
    found.extend(loc for loc in work.get("locations") or [] if loc)
    return found


def _venue(locations: list[Mapping[str, Any]]) -> str | None:
    """Prefer a journal / conference over a repository (e.g. arXiv) as the venue."""
    fallback: str | None = None
    for location in locations:
        source = location.get("source") or {}
        name = source.get("display_name")
        if not name:
            continue
        if source.get("type") != "repository":
            return str(name)
        fallback = fallback or str(name)
    return fallback


def _arxiv_from_locations(
    locations: list[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    """(arXiv ID, landing page URL) of the first arXiv location, if any."""
    for location in locations:
        for key in ("landing_page_url", "pdf_url"):
            arxiv_id = normalize_arxiv_id(location.get(key))
            if arxiv_id:
                return arxiv_id, location.get("landing_page_url") or location[key]
    return None, None


def parse_work(work: Mapping[str, Any]) -> PaperMeta | None:
    """Convert one OpenAlex work into a ``PaperMeta`` (``None`` if it has no title)."""
    title = (work.get("title") or work.get("display_name") or "").strip()
    openalex_id = openalex_short_id(work.get("id"))
    if not title or not openalex_id:
        return None

    doi = normalize_doi(work.get("doi"))
    locations = _locations(work)
    loc_arxiv_id, arxiv_url = _arxiv_from_locations(locations)
    arxiv_id = arxiv_id_from_doi(doi) or loc_arxiv_id

    links: dict[str, str] = {"openalex": work["id"]}
    if work.get("doi"):
        links["doi"] = work["doi"]
    if arxiv_url:
        links["arxiv"] = arxiv_url
    landing = (work.get("primary_location") or {}).get("landing_page_url")
    if landing:
        links["landing_page"] = landing
    oa_url = (work.get("open_access") or {}).get("oa_url")
    if oa_url:
        links["open_access"] = oa_url

    oa_pdf = (work.get("best_oa_location") or {}).get("pdf_url")
    if not oa_pdf:
        oa_pdf = next((loc["pdf_url"] for loc in locations if loc.get("pdf_url")), None)

    authors = tuple(
        name
        for a in work.get("authorships") or []
        if (name := (a.get("author") or {}).get("display_name"))
    )
    return PaperMeta(
        source="openalex",
        source_id=openalex_id,
        title=title,
        abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
        authors=authors,
        year=work.get("publication_year"),
        date=work.get("publication_date"),
        venue=_venue(locations),
        doi=doi,
        arxiv_id=arxiv_id,
        openalex_id=openalex_id,
        citation_count=work.get("cited_by_count"),
        links=links,
        oa_pdf_url=oa_pdf,
        work_type=work.get("type"),
        raw=dict(work),
    )


def _parse_works(results: list[Mapping[str, Any]]) -> list[PaperMeta]:
    return [p for w in results if (p := parse_work(w)) is not None]


class OpenAlexCollector(HttpCollector):
    name = "openalex"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        email: str | None = None,
        api_key: str | None = None,
        min_interval: float = 0.2,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, min_interval=min_interval, **kwargs)
        self._email = email
        self._api_key = api_key
        self.credits_limit: int | None = None
        self.credits_remaining: int | None = None
        self.credits_used = 0

    def _secrets(self) -> dict[str, str]:
        secrets: dict[str, str] = {}
        if self._email:
            secrets["mailto"] = self._email
        if self._api_key:
            secrets["api_key"] = self._api_key
        return secrets

    def _observe(self, response: httpx.Response) -> None:
        headers = response.headers
        remaining = _to_int(headers.get("x-ratelimit-remaining"))
        if remaining is not None:
            self.credits_remaining = remaining
        limit = _to_int(headers.get("x-ratelimit-limit"))
        if limit is not None:
            self.credits_limit = limit
        self.credits_used += _to_int(headers.get("x-ratelimit-credits-used")) or 0

    async def _json(
        self,
        path: str,
        params: Mapping[str, Any],
        ok_statuses: tuple[int, ...] = (200,),
    ) -> tuple[int, dict[str, Any]]:
        response = await self._get(
            f"{BASE_URL}{path}",
            params=params,
            secret_params=self._secrets(),
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
        """One page of results. ``offset`` must be a multiple of ``limit``."""
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= limit <= MAX_PER_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_PER_PAGE}")
        if offset % limit:
            raise ValueError("offset must be a multiple of limit for OpenAlex paging")
        if offset + limit > MAX_PAGEABLE:
            raise ValueError(
                f"OpenAlex page paging is limited to the first {MAX_PAGEABLE} results"
            )
        params: dict[str, Any] = {
            "search": query,
            "per-page": limit,
            "page": offset // limit + 1,
            "select": SELECT,
        }
        if since is not None:
            params["filter"] = f"from_publication_date:{since.isoformat()}"
        _, body = await self._json("/works", params)
        results = body.get("results") or []
        total = (body.get("meta") or {}).get("count")
        reached = offset + len(results)
        # A short page means the end; otherwise continue until the paging cap.
        more = len(results) == limit and reached < min(total or 0, MAX_PAGEABLE)
        return Page(
            items=_parse_works(results),
            total=total,
            next_offset=reached if more else None,
        )

    def _work_path(self, paper: PaperMeta) -> str | None:
        if paper.openalex_id:
            return f"/works/{paper.openalex_id}"
        if paper.doi:
            return f"/works/doi:{paper.doi}"
        if paper.arxiv_id:
            return f"/works/doi:10.48550/arxiv.{paper.arxiv_id}"
        return None

    async def references(self, paper: PaperMeta, limit: int) -> list[PaperMeta]:
        """Works that ``paper`` cites (a single-work lookup costs no credits)."""
        path = self._work_path(paper)
        if path is None or limit < 1:
            return []
        status, body = await self._json(
            path, {"select": "id,referenced_works"}, ok_statuses=(200, 404)
        )
        if status == 404:
            return []
        ids = [
            short
            for url in (body.get("referenced_works") or [])[:limit]
            if (short := openalex_short_id(url))
        ]
        found: list[PaperMeta] = []
        for start in range(0, len(ids), ID_BATCH):
            chunk = ids[start : start + ID_BATCH]
            _, page = await self._json(
                "/works",
                {
                    "filter": "openalex:" + "|".join(chunk),
                    "per-page": len(chunk),
                    "select": SELECT,
                },
            )
            found.extend(_parse_works(page.get("results") or []))
        return found

    async def citations(self, paper: PaperMeta, limit: int) -> list[PaperMeta]:
        """Works that cite ``paper``, most cited first."""
        if limit < 1:
            return []
        work_id = paper.openalex_id
        if work_id is None:
            path = self._work_path(paper)
            if path is None:
                return []
            status, body = await self._json(path, {"select": "id"}, (200, 404))
            work_id = openalex_short_id(body.get("id")) if status == 200 else None
            if work_id is None:
                return []
        per_page = min(limit, MAX_PER_PAGE)
        found: list[PaperMeta] = []
        page_number = 1
        while len(found) < limit and page_number * per_page <= MAX_PAGEABLE:
            _, body = await self._json(
                "/works",
                {
                    "filter": f"cites:{work_id}",
                    "sort": "cited_by_count:desc",
                    "per-page": per_page,
                    "page": page_number,
                    "select": SELECT,
                },
            )
            results = body.get("results") or []
            found.extend(_parse_works(results))
            if len(results) < per_page:
                break
            page_number += 1
        return found[:limit]


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
