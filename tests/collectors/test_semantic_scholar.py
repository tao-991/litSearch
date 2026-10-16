from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
import pytest

from litsearch.collectors.base import CollectorError, SourceStatus
from litsearch.collectors.semantic_scholar import (
    SemanticScholarCollector,
    parse_paper,
)
from litsearch.models import PaperMeta
from tests.collectors.helpers import FakeTime, Server, fixture_json, json_response


def make(server: Server, **kwargs: Any) -> SemanticScholarCollector:
    ft = FakeTime()
    return SemanticScholarCollector(
        server.client(), min_interval=0.0, sleep=ft.sleep, clock=ft.clock, **kwargs
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def paper(**kwargs: Any) -> PaperMeta:
    base = {"source": "s2", "source_id": "x", "title": "t"}
    return PaperMeta(**{**base, **kwargs})


def params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


# ---------------------------------------------------------------- pure parsing


def test_parse_published_paper_with_arxiv_copy() -> None:
    data = fixture_json("s2_search.json")["data"][0]
    p = parse_paper(data)
    assert p is not None
    assert p.source == "s2"
    assert p.s2_id == p.source_id == data["paperId"]
    assert p.title == data["title"]
    assert p.abstract == data["abstract"]
    assert p.doi == data["externalIds"]["DOI"].lower()
    assert p.arxiv_id == data["externalIds"]["ArXiv"]
    assert p.year == data["year"]
    assert p.date == data["publicationDate"]
    assert p.venue == data["venue"]
    assert p.oa_pdf_url == data["openAccessPdf"]["url"]
    assert p.citation_count == data["citationCount"]
    assert p.links["semantic_scholar"] == data["url"]
    assert p.links["doi"] == f"https://doi.org/{p.doi}"
    assert p.links["arxiv"] == f"https://arxiv.org/abs/{p.arxiv_id}"
    assert len(p.authors) == 2


def test_parse_paper_with_missing_fields() -> None:
    data = fixture_json("s2_search.json")["data"][1]
    assert data["abstract"] is None
    p = parse_paper(data)
    assert p is not None
    assert p.abstract is None
    assert p.year is None
    assert p.date is None
    assert p.venue is None
    assert p.arxiv_id is None


def test_parse_arxiv_only_paper() -> None:
    p = parse_paper(fixture_json("s2_search.json")["data"][2])
    assert p is not None
    assert p.doi is None
    assert p.arxiv_id is not None
    assert "doi" not in p.links


def test_unmatched_or_untitled_papers_are_skipped() -> None:
    assert parse_paper(None) is None
    assert parse_paper({"paperId": None, "title": "Unmatched"}) is None
    assert parse_paper({"paperId": "abc", "title": "  "}) is None


# ---------------------------------------------------------------------- search


def test_search_parameters_and_api_key_header() -> None:
    server = Server([json_response(fixture_json("s2_search.json"))])
    c = make(server, api_key="KEY123")
    page = run(c.search("coding agents", limit=4, since=date(2024, 5, 1)))
    request = server.requests[0]
    assert request.url.path == "/graph/v1/paper/search"
    sent = params(request)
    assert sent["query"] == "coding agents"
    assert sent["limit"] == "4"
    assert sent["offset"] == "0"
    assert sent["year"] == "2024-"
    assert "abstract" in sent["fields"]
    assert request.headers["x-api-key"] == "KEY123"
    assert len(page.items) == 4
    assert page.total == 1035
    assert page.next_offset == 20


def test_search_without_key_sends_no_key_header() -> None:
    server = Server([json_response(fixture_json("s2_search.json"))])
    run(make(server).search("q", limit=4))
    assert "x-api-key" not in server.requests[0].headers
    assert "year" not in params(server.requests[0])


def test_limit_is_capped_at_100_per_request() -> None:
    server = Server([json_response({"data": [], "total": 0})])
    run(make(server).search("q", limit=500))
    assert params(server.requests[0])["limit"] == "100"


def test_window_never_exceeds_the_999_result_cap() -> None:
    server = Server([json_response({"data": [], "total": 5000, "next": 1000})])
    page = run(make(server).search("q", limit=100, offset=950))
    assert params(server.requests[0])["limit"] == "49"  # 950 + 49 = 999
    assert page.next_offset is None  # 1000 is out of reach


def test_search_beyond_the_cap_makes_no_request() -> None:
    server = Server()
    page = run(make(server).search("q", limit=20, offset=999))
    assert page.items == []
    assert page.next_offset is None
    assert server.requests == []


@pytest.mark.parametrize(("query", "limit", "offset"), [("", 10, 0), ("q", 0, 0)])
def test_invalid_search_arguments(query: str, limit: int, offset: int) -> None:
    with pytest.raises(ValueError):
        run(make(Server()).search(query, limit=limit, offset=offset))


def test_rate_limiting_is_reported_not_raised_as_a_crash() -> None:
    server = Server([json_response({"message": "Too Many Requests"}, 429)] * 4)
    c = make(server)
    with pytest.raises(CollectorError) as info:
        run(c.search("q", limit=4))
    assert info.value.status is SourceStatus.RATE_LIMITED


# ------------------------------------------------------ references and citations


def test_references_use_the_cited_paper_and_skip_unmatched_ones() -> None:
    server = Server([json_response(fixture_json("s2_references.json"))])
    result = run(make(server).references(paper(s2_id="abc123"), limit=10))
    assert server.requests[0].url.path == "/graph/v1/paper/abc123/references"
    assert len(result) == 2  # the entry without a paperId is dropped
    assert all(isinstance(p, PaperMeta) for p in result)


def test_citations_use_the_citing_paper() -> None:
    server = Server([json_response(fixture_json("s2_citations.json"))])
    result = run(make(server).citations(paper(s2_id="abc123"), limit=10))
    assert server.requests[0].url.path == "/graph/v1/paper/abc123/citations"
    assert len(result) == 1


def test_related_papers_can_be_found_by_doi_or_arxiv_id() -> None:
    by_doi = Server([json_response({"data": []})])
    run(make(by_doi).references(paper(doi="10.1000/abc"), limit=5))
    assert by_doi.requests[0].url.path == "/graph/v1/paper/DOI:10.1000/abc/references"

    by_arxiv = Server([json_response({"data": []})])
    run(make(by_arxiv).citations(paper(arxiv_id="2601.21379"), limit=5))
    assert by_arxiv.requests[0].url.path == (
        "/graph/v1/paper/ARXIV:2601.21379/citations"
    )


def test_related_papers_follow_pagination_until_the_limit() -> None:
    first = fixture_json("s2_references.json")
    first["next"] = 100
    second = fixture_json("s2_references.json")
    server = Server([json_response(first), json_response(second)])
    result = run(make(server).references(paper(s2_id="abc"), limit=3))
    assert [params(r)["offset"] for r in server.requests] == ["0", "100"]
    assert len(result) == 3  # 2 + 2 found, trimmed to the limit


def test_unknown_paper_gives_no_related_papers() -> None:
    server = Server([json_response({"error": "not found"}, 404)])
    assert run(make(server).references(paper(s2_id="nope"), limit=5)) == []


def test_related_papers_without_identifier_make_no_request() -> None:
    server = Server()
    assert run(make(server).citations(paper(), limit=5)) == []
    assert server.requests == []
