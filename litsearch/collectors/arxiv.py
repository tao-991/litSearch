"""arXiv collector (Atom API): search only; arXiv offers no citation data.

Rules confirmed in Phase 0: at most one request at a time and at least 3 seconds
between requests. Queries need boolean syntax (``all:term AND all:"a phrase"``), so a
plain phrase is converted by ``to_arxiv_query``. Date filtering through
``submittedDate`` is implemented but was not verified live.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

import httpx

from litsearch.collectors.base import CollectorError, HttpCollector, SourceStatus
from litsearch.models import Page, PaperMeta
from litsearch.normalize import normalize_arxiv_id, normalize_doi

API_URL = "https://export.arxiv.org/api/query"
MAX_PER_PAGE = 200
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
    "ax": "http://arxiv.org/schemas/atom",
}
_STOPWORDS = frozenset(
    "a an and are as at be by for from how in into is it of on or that the to "
    "using via what when where which with without".split()
)
_QUOTED_OR_WORD = re.compile(r'"([^"]+)"|(\S+)')


def to_arxiv_query(phrase: str) -> str:
    """Turn a plain phrase into an arXiv query where every keyword must match.

    ``coding agent pull-request review`` ->
    ``all:coding AND all:agent AND all:"pull-request" AND all:review``.
    Quoted phrases are kept as phrases; stopwords are dropped.
    """
    terms: list[str] = []
    for quoted, word in _QUOTED_OR_WORD.findall(phrase):
        if quoted:
            text = " ".join(re.sub(r'["\\]', " ", quoted).split())
            if text:
                terms.append(f'all:"{text}"')
            continue
        cleaned = re.sub(r"[^\w\-]", "", word)
        if not cleaned.strip("-") or cleaned.lower() in _STOPWORDS:
            continue
        terms.append(f'all:"{cleaned}"' if "-" in cleaned else f"all:{cleaned}")
    if not terms:
        raise ValueError("query has no searchable words")
    return " AND ".join(terms)


def _text(entry: ET.Element, path: str) -> str | None:
    value = entry.findtext(path, namespaces=NS)
    if value is None:
        return None
    return " ".join(value.split()) or None


def parse_feed(xml_text: str) -> tuple[int | None, list[PaperMeta]]:
    """Parse an Atom feed into (reported total, papers).

    Raises ``CollectorError`` if the text is not XML or is arXiv's error feed.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise CollectorError(
            SourceStatus.ERROR, "arxiv: response is not valid XML"
        ) from None
    total_text = root.findtext("os:totalResults", namespaces=NS)
    total = int(total_text) if total_text and total_text.isdigit() else None

    papers: list[PaperMeta] = []
    for entry in root.findall("a:entry", NS):
        entry_id = _text(entry, "a:id") or ""
        if "/api/errors" in entry_id:
            detail = _text(entry, "a:summary") or "unknown error"
            raise CollectorError(SourceStatus.ERROR, f"arxiv: {detail}")
        paper = _parse_entry(entry, entry_id)
        if paper is not None:
            papers.append(paper)
    return total, papers


def _parse_entry(entry: ET.Element, entry_id: str) -> PaperMeta | None:
    arxiv_id = normalize_arxiv_id(entry_id)
    title = _text(entry, "a:title")
    if arxiv_id is None or title is None:
        return None
    doi = normalize_doi(_text(entry, "ax:doi"))
    journal_ref = _text(entry, "ax:journal_ref")
    published = _text(entry, "a:published")
    links = {
        (link.get("title") or link.get("rel") or ""): link.get("href") or ""
        for link in entry.findall("a:link", NS)
    }
    abs_link = links.get("alternate") or entry_id
    pdf_link = links.get("pdf")

    out_links = {"arxiv": abs_link}
    if pdf_link:
        out_links["pdf"] = pdf_link
    if doi:
        out_links["doi"] = f"https://doi.org/{doi}"
    return PaperMeta(
        source="arxiv",
        source_id=arxiv_id,
        title=title,
        abstract=_text(entry, "a:summary"),
        authors=tuple(
            name for a in entry.findall("a:author", NS) if (name := _text(a, "a:name"))
        ),
        year=int(published[:4]) if published and published[:4].isdigit() else None,
        date=published[:10] if published else None,
        venue=journal_ref,
        doi=doi,
        arxiv_id=arxiv_id,
        links=out_links,
        oa_pdf_url=pdf_link,
        work_type="preprint",
        raw={
            "id": entry_id,
            "updated": _text(entry, "a:updated"),
            "comment": _text(entry, "ax:comment"),
            "journal_ref": journal_ref,
            "primary_category": (
                pc.get("term")
                if (pc := entry.find("ax:primary_category", NS)) is not None
                else None
            ),
        },
    )


class ArxivCollector(HttpCollector):
    name = "arxiv"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        min_interval: float = 3.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, min_interval=min_interval, **kwargs)

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        since: date | None = None,
    ) -> Page:
        """One page of relevance-ranked results for a plain phrase."""
        if limit < 1 or offset < 0:
            raise ValueError("limit must be >= 1 and offset >= 0")
        limit = min(limit, MAX_PER_PAGE)
        search_query = to_arxiv_query(query)
        if since is not None:
            window = f"submittedDate:[{since:%Y%m%d}0000 TO 209912312359]"
            search_query = f"({search_query}) AND {window}"
        response = await self._get(
            API_URL,
            params={
                "search_query": search_query,
                "start": offset,
                "max_results": limit,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        total, papers = parse_feed(response.text)
        reached = offset + len(papers)
        more = len(papers) == limit and total is not None and reached < total
        return Page(items=papers, total=total, next_offset=reached if more else None)
