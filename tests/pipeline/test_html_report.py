from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from litsearch import db
from litsearch.models import PaperMeta
from litsearch.pipeline.export import load_candidates, load_summary
from litsearch.pipeline.html_report import render_html, write_html
from litsearch.pipeline.score import context_hash
from litsearch.pipeline.store import RunStore, add_queries

# reuse the runs built for the Markdown tests
from tests.pipeline.test_export import add_scores, filled, scored  # noqa: F401


def html_of(conn: sqlite3.Connection, run_id: int, **kwargs: int) -> str:
    return render_html(
        load_candidates(conn, run_id), load_summary(conn, run_id), **kwargs
    )


def test_the_page_is_a_complete_document_with_one_block_per_query(scored) -> None:  # noqa: F811
    conn, run_id = scored
    page = html_of(conn, run_id)
    assert page.startswith("<!doctype html>")
    assert "<title>run-" in page or "<title>" in page
    assert 'name="viewport"' in page
    first = page.index('<section class="query" id="q1">')
    second = page.index('<section class="query" id="q2">')
    assert first < second
    assert "1. agents review" in page and "2. human in the loop" in page
    block = page[first:second]
    assert "Trust In Automation" in block
    assert "Trust In Automation" not in page[second:].split("</section>")[0]
    assert '<div class="empty">No paper fits this query best' in page[second:]


def test_cards_carry_score_text_and_filter_data(scored) -> None:  # noqa: F811
    conn, run_id = scored
    page = html_of(conn, run_id)
    card = re.search(
        r'<article class="paper s5" data-score="5"[^>]*>.*?</article>', page, re.S
    )
    assert card is not None
    text = card.group(0)
    assert 'title="score 5 of 5">5</div>' in text
    assert "Trust In Automation" in text
    assert "<b>Fits</b> directly about trust" in text
    assert "based on the abstract" in text
    assert '<span class="tag">tag one</span>' in text
    assert 'data-text="' in text and "trust in automation" in text.lower()


def test_possibly_relevant_papers_are_collapsed_and_screened_out_ones_are_a_table(
    scored,  # noqa: F811
) -> None:
    conn, run_id = scored
    page = html_of(conn, run_id)
    assert (
        '<details class="maybe"><summary>Possibly relevant (score 3): 1</summary>'
        in page
    )
    assert re.search(
        r'<details class="maybe">.*Agents That Review Code.*</details>', page, re.S
    )
    assert '<details class="screened"><summary>Screened out' in page
    assert re.search(r'<tr class="paper" data-score="1"[^>]*><td>1</td>', page)
    assert "only shares words | and a pipe" in page  # a pipe is fine in HTML


def test_the_default_filter_shows_relevant_and_possibly_relevant(scored) -> None:  # noqa: F811
    conn, run_id = scored
    page = html_of(conn, run_id)
    select = re.search(r'<select id="minScore">(.*?)</select>', page, re.S).group(1)
    assert '<option value="3" selected>3+ with possibly relevant</option>' in select
    assert '<option value="5">5 only</option>' in select
    assert '<option value="4">4+ relevant</option>' in select
    assert '<option value="0">all, including screened out</option>' in select
    custom = html_of(conn, run_id, min_score=5, maybe_score=2)
    assert '<option value="2" selected>2+ with possibly relevant</option>' in custom


def test_the_header_counts_and_says_which_model_scored(scored) -> None:  # noqa: F811
    conn, run_id = scored
    add_scores(conn, run_id, {})  # no-op, keeps the fixture import used
    page = html_of(conn, run_id)
    assert "2 queries" in page and "3 papers found" in page and "3 scored" in page
    assert "1 relevant (4+)" in page and "1 possibly relevant (3)" in page
    assert "<code>m, prompt v</code>" in page
    assert "Small models score generously" in page


def test_everything_from_sources_and_models_is_escaped(scored) -> None:  # noqa: F811
    conn, run_id = scored
    nasty = '<script>alert("x")</script> & "quotes"'
    with conn:
        conn.execute(
            "UPDATE papers SET title = ? WHERE title = 'Trust In Automation'", (nasty,)
        )
        conn.execute("UPDATE scores SET reason = ?, summary = ?", (nasty, nasty))
        conn.execute(
            "UPDATE papers SET venue = ?, authors = ?", (nasty, db.dumps_json([nasty]))
        )
    page = html_of(conn, run_id)
    assert page.count("<script>") == 1  # only our own filter script
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert "<script>alert" not in page  # the injected tag never appears raw


def test_only_http_links_are_used(scored) -> None:  # noqa: F811
    conn, run_id = scored
    evil = db.dumps_json(
        {
            "doi": "javascript:alert(1)",
            "arxiv": "data:text/html,boom",
            "landing_page": "JAVASCRIPT:void(0)",
            "openalex": "https://openalex.org/W1",
        }
    )
    with conn:
        conn.execute(
            "UPDATE papers SET links = ?, oa_pdf_url = 'javascript:void(0)'", (evil,)
        )
    page = html_of(conn, run_id).lower()
    assert "javascript:" not in page
    assert "data:text" not in page
    assert (
        'href="https://openalex.org/w1"' in page
    )  # the safe one is kept as the title link


def test_the_page_needs_no_network_and_loads_nothing_external(scored) -> None:  # noqa: F811
    conn, run_id = scored
    page = html_of(conn, run_id)
    assert "<link " not in page
    assert " src=" not in page
    assert "@import" not in page and "url(" not in page


def test_a_paper_without_any_link_is_shown_as_plain_text(scored) -> None:  # noqa: F811
    conn, run_id = scored
    with conn:
        conn.execute("UPDATE papers SET links = '{}', oa_pdf_url = NULL")
    page = html_of(conn, run_id)
    assert "<h3>Trust In Automation</h3>" in page


def test_an_unscored_run_lists_candidates_without_scores(filled) -> None:  # noqa: F811
    conn, run_id = filled
    page = html_of(conn, run_id)
    assert "Not scored." in page
    assert "<h2>Candidates</h2>" in page
    assert 'data-score="-1"' in page and ">\u2013</div>" in page
    assert '<option value="0" selected>' in page
    assert "Possibly relevant" not in page and 'class="toc"' not in page


def test_papers_that_were_not_scored_are_pointed_out(filled) -> None:  # noqa: F811
    conn, run_id = filled
    add_scores(conn, run_id, {"Agents That Review Code": (4, "fine")})
    page = html_of(conn, run_id)
    assert "2 papers were not scored" in page
    assert f"litsearch score {run_id}" in page


def test_a_paper_judged_by_its_title_says_so(filled) -> None:  # noqa: F811
    conn, run_id = filled
    add_scores(conn, run_id, {"Agents That Review Code": (4, "fine")}, basis="title")
    assert "based on the title only, no abstract" in html_of(conn, run_id)


def test_write_html_creates_the_file_and_folders(scored, tmp_path: Path) -> None:  # noqa: F811
    conn, run_id = scored
    path = tmp_path / "out" / "run" / "candidates.html"
    write_html(load_candidates(conn, run_id), load_summary(conn, run_id), path)
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_the_filter_script_is_present_and_reads_the_data_attributes(scored) -> None:  # noqa: F811
    conn, run_id = scored
    page = html_of(conn, run_id)
    script = page.split("<script>")[1]
    assert "dataset.score" in script and "dataset.text" in script
    assert "minScore" in script and "details.maybe" in script


def test_a_query_that_returned_results_but_has_no_relevant_paper_says_so(
    tmp_path: Path,
) -> None:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    run_id = db.create_run(conn, "h")
    (qid, _), _ = add_queries(conn, run_id, ["lonely query", "other query"])
    paper = PaperMeta(
        source="openalex",
        source_id="W1",
        title="Some paper",
        authors=("A B",),
        year=2024,
    )
    RunStore(conn, run_id).ingest([paper], query_id=qid, source="openalex")
    paper_id = conn.execute("SELECT id FROM papers").fetchone()[0]
    ctx = context_hash(["lonely query", "other query"])
    with conn:
        conn.execute(
            "INSERT INTO scores (paper_id, project_hash, input_hash, prompt_version, "
            "model, score, reason, summary, tags, matched_query) "
            "VALUES (?, ?, 'i', 'v', 'm', 1, 'r', 's', '[]', 'lonely query')",
            (paper_id, ctx),
        )
    page = render_html(load_candidates(conn, run_id), load_summary(conn, run_id))
    assert "1 results · 0 relevant · 0 possibly relevant" in page
    assert page.count("No paper fits this query best") == 2
