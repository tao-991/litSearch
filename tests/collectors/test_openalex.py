from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any

import httpx
import pytest

from litsearch.collectors.base import CollectorError, SourceStatus
from litsearch.collectors.openalex import (
    OpenAlexCollector,
    estimate_credits,
    parse_work,
    reconstruct_abstract,
)
from litsearch.models import PaperMeta
from litsearch.normalize import normalize_arxiv_id
from tests.collectors.helpers import (
    FakeTime,
    Server,
    fixture_json,
    json_response,
)


def make(server: Server, **kwargs: Any) -> OpenAlexCollector:
    ft = FakeTime()
    return OpenAlexCollector(
        server.client(), min_interval=0.0, sleep=ft.sleep, clock=ft.clock, **kwargs
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def paper(**kwargs: Any) -> PaperMeta:
    base = {"source": "openalex", "source_id": "x", "title": "t"}
    return PaperMeta(**{**base, **kwargs})


def params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


# ---------------------------------------------------------------- pure parsing


def test_reconstruct_abstract_orders_words_by_position() -> None:
    assert reconstruct_abstract({"b": [1], "a": [0], "c": [2]}) == "a b c"
    assert reconstruct_abstract({"the": [0, 3], "cat": [1], "sat": [2]}) == (
        "the cat sat the"
    )
    assert reconstruct_abstract({}) is None
    assert reconstruct_abstract(None) is None


def test_parse_conference_paper_with_arxiv_copy() -> None:
    work = fixture_json("openalex_search.json")["results"][0]
    p = parse_work(work)
    assert p is not None
    assert p.source == "openalex"
    assert p.openalex_id == p.source_id
    assert re.fullmatch(r"W\d+", p.openalex_id or "")
    assert p.doi == "10.1109/compsac69091.2026.00052"
    assert p.venue == (
        "IEEE Annual Computers, Software, and Applications Conference (COMPSAC)"
    )
    assert p.work_type == "conference-paper"
    assert p.abstract and p.abstract.strip() == p.abstract
    assert len(p.authors) == 2
    assert p.year == work["publication_year"]
    assert p.date == work["publication_date"]
    assert p.citation_count == work["cited_by_count"]
    # the arXiv ID comes from the arXiv location, not from the DOI
    arxiv_locs = [
        normalize_arxiv_id(loc.get("landing_page_url"))
        for loc in work["locations"]
        if normalize_arxiv_id(loc.get("landing_page_url"))
    ]
    assert p.arxiv_id == arxiv_locs[0]
    assert p.links["openalex"] == work["id"]
    assert p.links["doi"] == work["doi"]
    assert "arxiv.org" in p.links["arxiv"]
    assert p.raw["id"] == work["id"]


def test_parse_preprint_without_doi_gets_arxiv_id_from_location() -> None:
    work = fixture_json("openalex_search.json")["results"][1]
    assert work["doi"] is None
    p = parse_work(work)
    assert p is not None
    assert p.doi is None
    assert p.arxiv_id and re.fullmatch(r"\d{4}\.\d{4,5}", p.arxiv_id)
    assert p.venue == "arXiv (Cornell University)"
    assert p.work_type == "preprint"
    assert "doi" not in p.links


def test_parse_arxiv_doi_gives_arxiv_id() -> None:
    work = fixture_json("openalex_search.json")["results"][2]
    p = parse_work(work)
    assert p is not None
    assert p.doi == "10.48550/arxiv.2608.21311"
    assert p.arxiv_id == "2608.21311"


def test_parse_journal_article_without_abstract() -> None:
    work = fixture_json("openalex_search.json")["results"][3]
    p = parse_work(work)
    assert p is not None
    assert p.abstract is None
    assert p.venue == "Information and Software Technology"
    assert p.doi == "10.1016/j.infsof.2026.108333"
    assert p.arxiv_id is None


def test_work_without_title_or_id_is_skipped() -> None:
    assert parse_work({"id": "https://openalex.org/W1", "title": None}) is None
    assert parse_work({"title": "No id"}) is None


def test_estimate_credits() -> None:
    assert estimate_credits(30) == 300
    assert estimate_credits(30, 80) == 380


# ---------------------------------------------------------------------- search


def test_search_sends_expected_parameters_and_credentials() -> None:
    server = Server([json_response(fixture_json("openalex_search.json"))])
    c = make(server, email="me@example.org", api_key="KEY123")
    page = run(c.search("coding agents", limit=4, offset=0, since=date(2024, 1, 1)))
    sent = params(server.requests[0])
    assert server.requests[0].url.path == "/works"
    assert sent["search"] == "coding agents"
    assert sent["per-page"] == "4"
    assert sent["page"] == "1"
    assert sent["filter"] == "from_publication_date:2024-01-01"
    assert "abstract_inverted_index" in sent["select"]
    assert sent["mailto"] == "me@example.org"
    assert sent["api_key"] == "KEY123"
    assert len(page.items) == 4
    assert page.total == 35985


def test_search_without_credentials_sends_none() -> None:
    server = Server([json_response(fixture_json("openalex_search.json"))])
    c = make(server)
    run(c.search("coding agents", limit=4))
    sent = params(server.requests[0])
    assert "mailto" not in sent
    assert "api_key" not in sent
    assert "filter" not in sent


def test_paging_uses_page_numbers_and_reports_next_offset() -> None:
    body = fixture_json("openalex_search.json")
    server = Server([json_response(body), json_response(body)])
    c = make(server)
    first = run(c.search("q", limit=4, offset=0))
    third = run(c.search("q", limit=4, offset=8))
    assert params(server.requests[0])["page"] == "1"
    assert params(server.requests[1])["page"] == "3"
    assert first.next_offset == 4
    assert third.next_offset == 12


def test_short_page_means_no_next_offset() -> None:
    server = Server([json_response(fixture_json("openalex_search.json"))])
    page = run(make(server).search("q", limit=20))  # only 4 results come back
    assert len(page.items) == 4
    assert page.next_offset is None


def test_last_page_has_no_next_offset() -> None:
    body = fixture_json("openalex_search.json")
    body["meta"]["count"] = 4
    server = Server([json_response(body)])
    page = run(make(server).search("q", limit=4))
    assert page.next_offset is None


@pytest.mark.parametrize(
    ("query", "limit", "offset"),
    [
        ("", 20, 0),
        ("   ", 20, 0),
        ("q", 0, 0),
        ("q", 201, 0),
        ("q", 20, 5),
        ("q", 200, 9_900),
    ],
)
def test_invalid_search_arguments_are_rejected_without_a_request(
    query: str, limit: int, offset: int
) -> None:
    server = Server()
    with pytest.raises(ValueError):
        run(make(server).search(query, limit=limit, offset=offset))
    assert server.requests == []


def test_credit_headers_are_tracked() -> None:
    body = fixture_json("openalex_search.json")
    headers = {
        "x-ratelimit-limit": "1000",
        "x-ratelimit-remaining": "990",
        "x-ratelimit-credits-used": "10",
    }
    later = {**headers, "x-ratelimit-remaining": "980"}
    server = Server([json_response(body, **headers), json_response(body, **later)])
    c = make(server)
    run(c.search("a", limit=4))
    assert (c.credits_limit, c.credits_remaining, c.credits_used) == (1000, 990, 10)
    run(c.search("b", limit=4))
    assert (c.credits_remaining, c.credits_used) == (980, 20)


def test_quota_exhaustion_is_reported_as_rate_limited() -> None:
    server = Server([json_response({"error": "quota"}, 429)] * 4)
    with pytest.raises(CollectorError) as info:
        run(make(server).search("q", limit=4))
    assert info.value.status is SourceStatus.RATE_LIMITED


# ------------------------------------------------------ references and citations


def test_references_look_up_the_work_then_fetch_the_cited_works() -> None:
    refs = fixture_json("openalex_work_refs.json")
    server = Server(
        [
            json_response(refs),
            json_response(fixture_json("openalex_refs_batch.json")),
        ]
    )
    result = run(make(server).references(paper(openalex_id="W7168370872"), limit=50))
    lookup, batch = server.requests
    assert lookup.url.path == "/works/W7168370872"
    assert params(lookup)["select"] == "id,referenced_works"
    assert batch.url.path == "/works"
    wanted = [u.rsplit("/", 1)[-1] for u in refs["referenced_works"]]
    assert params(batch)["filter"] == "openalex:" + "|".join(wanted)
    assert len(result) == 3
    assert all(isinstance(p, PaperMeta) for p in result)


def test_references_respect_the_limit() -> None:
    refs = fixture_json("openalex_work_refs.json")
    server = Server([json_response(refs), json_response({"results": []})])
    run(make(server).references(paper(openalex_id="W1"), limit=2))
    wanted = [u.rsplit("/", 1)[-1] for u in refs["referenced_works"][:2]]
    assert params(server.requests[1])["filter"] == "openalex:" + "|".join(wanted)


def test_references_of_an_unknown_work_are_empty() -> None:
    server = Server([json_response({"error": "not found"}, 404)])
    assert run(make(server).references(paper(openalex_id="W1"), limit=5)) == []


def test_references_without_any_identifier_make_no_request() -> None:
    server = Server()
    assert run(make(server).references(paper(), limit=5)) == []
    assert server.requests == []


def test_references_can_find_a_work_by_doi_or_arxiv_id() -> None:
    by_doi = Server([json_response({"referenced_works": []})])
    run(make(by_doi).references(paper(doi="10.1000/abc"), limit=5))
    assert by_doi.requests[0].url.path == "/works/doi:10.1000/abc"

    by_arxiv = Server([json_response({"referenced_works": []})])
    run(make(by_arxiv).references(paper(arxiv_id="2601.21379"), limit=5))
    assert by_arxiv.requests[0].url.path == "/works/doi:10.48550/arxiv.2601.21379"


def test_citations_filter_on_cites_and_sort_by_citation_count() -> None:
    server = Server([json_response(fixture_json("openalex_cited_by.json"))])
    result = run(make(server).citations(paper(openalex_id="W42"), limit=5))
    sent = params(server.requests[0])
    assert sent["filter"] == "cites:W42"
    assert sent["sort"] == "cited_by_count:desc"
    assert len(result) == 2


def test_citations_stop_at_the_limit_without_extra_requests() -> None:
    body = fixture_json("openalex_cited_by.json")
    server = Server([json_response(body)])
    result = run(make(server).citations(paper(openalex_id="W42"), limit=2))
    assert len(result) == 2
    assert len(server.requests) == 1


def test_citations_follow_pages_until_a_short_page() -> None:
    body = fixture_json("openalex_cited_by.json")
    full_page = {**body, "results": body["results"] * 100}  # 200 results
    server = Server([json_response(full_page), json_response(body)])
    result = run(make(server).citations(paper(openalex_id="W42"), limit=250))
    assert [params(r)["page"] for r in server.requests] == ["1", "2"]
    assert [params(r)["per-page"] for r in server.requests] == ["200", "200"]
    assert len(result) == 202  # 200 + 2, the short second page ends the search


def test_citations_resolve_the_openalex_id_from_a_doi() -> None:
    server = Server(
        [
            json_response({"id": "https://openalex.org/W9"}),
            json_response(fixture_json("openalex_cited_by.json")),
        ]
    )
    run(make(server).citations(paper(doi="10.1000/abc"), limit=5))
    assert server.requests[0].url.path == "/works/doi:10.1000/abc"
    assert params(server.requests[1])["filter"] == "cites:W9"


def test_citations_of_an_unknown_work_are_empty() -> None:
    server = Server([json_response({}, 404)])
    assert run(make(server).citations(paper(doi="10.1000/abc"), limit=5)) == []
