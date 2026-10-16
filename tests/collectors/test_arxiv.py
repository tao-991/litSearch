from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
import pytest

from litsearch.collectors.arxiv import ArxivCollector, parse_feed, to_arxiv_query
from litsearch.collectors.base import CollectorError, SourceStatus
from tests.collectors.helpers import FakeTime, Server, fixture_text


def xml_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, text=text, headers={"content-type": "application/atom+xml"}
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make(server: Server, ft: FakeTime | None = None, **kwargs: Any) -> ArxivCollector:
    ft = ft or FakeTime()
    return ArxivCollector(server.client(), sleep=ft.sleep, clock=ft.clock, **kwargs)


# ------------------------------------------------------------- query conversion


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        (
            "coding agent pull request review",
            "all:coding AND all:agent AND all:pull AND all:request AND all:review",
        ),
        (
            "trust in the acceptance of AI-generated code changes",
            'all:trust AND all:acceptance AND all:"AI-generated" AND all:code '
            "AND all:changes",
        ),
        (
            '"human in the loop" software agents',
            'all:"human in the loop" AND all:software AND all:agents',
        ),
        ("LLM-based, agents?", 'all:"LLM-based" AND all:agents'),
        ("Agents OR everything", "all:Agents AND all:everything"),
    ],
)
def test_to_arxiv_query(phrase: str, expected: str) -> None:
    assert to_arxiv_query(phrase) == expected


@pytest.mark.parametrize("phrase", ["", "   ", "the of and", "!!! ???", "---"])
def test_query_without_searchable_words_is_rejected(phrase: str) -> None:
    with pytest.raises(ValueError):
        to_arxiv_query(phrase)


def test_query_cannot_smuggle_in_arxiv_syntax() -> None:
    converted = to_arxiv_query('agent" OR all:secret')
    assert converted == "all:agent AND all:allsecret"


# ---------------------------------------------------------------------- parsing


def test_parse_feed_extracts_papers_from_a_real_response() -> None:
    total, papers = parse_feed(fixture_text("arxiv_search.xml"))
    assert total == 108
    assert len(papers) == 4
    plain, with_doi, both, journal_only = papers
    for p in papers:
        assert p.source == "arxiv"
        assert p.source_id == p.arxiv_id
        assert p.work_type == "preprint"
        assert p.abstract and "\n" not in p.abstract
        assert len(p.authors) == 2
        assert p.year is not None
        assert p.date and len(p.date) == 10
        assert p.links["arxiv"].startswith("http")
        assert p.oa_pdf_url and "/pdf/" in p.oa_pdf_url
        assert p.raw["primary_category"]
    assert plain.arxiv_id == "2602.17084"  # version suffix removed
    assert plain.doi is None
    assert plain.venue is None
    assert with_doi.doi and with_doi.links["doi"] == f"https://doi.org/{with_doi.doi}"
    assert both.venue == both.raw["journal_ref"]
    assert journal_only.doi is None
    assert journal_only.venue


def test_parse_feed_empty_result() -> None:
    assert parse_feed(fixture_text("arxiv_empty.xml")) == (0, [])


def test_parse_feed_reports_arxiv_error_feed() -> None:
    with pytest.raises(CollectorError) as info:
        parse_feed(fixture_text("arxiv_error.xml"))
    assert info.value.status is SourceStatus.ERROR
    assert "incorrect id format" in info.value.message


def test_parse_feed_rejects_non_xml() -> None:
    with pytest.raises(CollectorError) as info:
        parse_feed("<html>maintenance</html")
    assert info.value.status is SourceStatus.ERROR


# ----------------------------------------------------------------------- search


def test_search_request_parameters() -> None:
    server = Server([xml_response(fixture_text("arxiv_search.xml"))])
    page = run(make(server).search("coding agent", limit=4, offset=8))
    request = server.requests[0]
    assert request.url.host == "export.arxiv.org"
    assert request.url.path == "/api/query"
    sent = dict(request.url.params)
    assert sent["search_query"] == "all:coding AND all:agent"
    assert sent["start"] == "8"
    assert sent["max_results"] == "4"
    assert sent["sortBy"] == "relevance"
    assert len(page.items) == 4
    assert page.total == 108
    assert page.next_offset == 12


def test_short_page_means_no_next_offset() -> None:
    server = Server([xml_response(fixture_text("arxiv_search.xml"))])
    page = run(make(server).search("coding agent", limit=20))
    assert len(page.items) == 4
    assert page.next_offset is None


def test_empty_result_page() -> None:
    server = Server([xml_response(fixture_text("arxiv_empty.xml"))])
    page = run(make(server).search("nothing matches", limit=20))
    assert page.items == []
    assert page.total == 0
    assert page.next_offset is None


def test_since_adds_a_submitted_date_filter() -> None:
    server = Server([xml_response(fixture_text("arxiv_empty.xml"))])
    run(make(server).search("coding agent", limit=5, since=date(2024, 3, 9)))
    query = server.requests[0].url.params["search_query"]
    assert query == (
        "(all:coding AND all:agent) AND submittedDate:[202403090000 TO 209912312359]"
    )


def test_page_size_is_capped() -> None:
    server = Server([xml_response(fixture_text("arxiv_empty.xml"))])
    run(make(server).search("coding agent", limit=5000))
    assert server.requests[0].url.params["max_results"] == "200"


def test_requests_are_spaced_at_least_three_seconds_apart() -> None:
    server = Server([xml_response(fixture_text("arxiv_empty.xml"))] * 3)
    ft = FakeTime()
    c = make(server, ft)

    async def scenario() -> None:
        await c.search("coding agent", limit=5)
        await c.search("coding agent", limit=5)
        await c.search("coding agent", limit=5)

    run(scenario())
    assert ft.sleeps == [3.0, 3.0]


def test_invalid_search_arguments_are_rejected_without_a_request() -> None:
    server = Server()
    with pytest.raises(ValueError):
        run(make(server).search("the of", limit=5))
    with pytest.raises(ValueError):
        run(make(server).search("agents", limit=0))
    assert server.requests == []


def test_server_trouble_is_reported() -> None:
    server = Server([xml_response("busy", 503)] * 4)
    with pytest.raises(CollectorError) as info:
        run(make(server).search("coding agent", limit=5))
    assert info.value.status is SourceStatus.ERROR
