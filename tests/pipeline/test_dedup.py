from __future__ import annotations

import itertools
from typing import Any

import pytest

from litsearch.models import PaperMeta
from litsearch.pipeline.dedup import (
    MERGE_THRESHOLD,
    SUSPECT_THRESHOLD,
    Deduplicator,
    deduplicate,
    merge_records,
    record_status,
    title_similarity,
)

_ids = itertools.count(1)


def meta(
    title: str = "A Study of Code Review Agents in Industry", **kwargs: Any
) -> PaperMeta:
    """A record with sensible defaults; every call gets a fresh source ID."""
    fields: dict[str, Any] = {
        "source": "openalex",
        "source_id": f"W{next(_ids)}",
        "title": title,
        "authors": ("Jing Jiang", "Ana Costa"),
        "year": 2025,
    }
    fields.update(kwargs)
    return PaperMeta(**fields)


def titles_of(result_papers: list[Any]) -> list[str]:
    return [p.title for p in result_papers]


# ------------------------------------------------------------ identifier matching


def test_same_doi_is_one_paper() -> None:
    a = meta("Title One About Agents", doi="10.1000/abc")
    b = meta("Completely Different Wording", doi="10.1000/abc", source="s2")
    result = deduplicate([a, b])
    assert len(result.papers) == 1
    assert len(result.papers[0].records) == 2


def test_same_arxiv_id_across_sources_is_one_paper() -> None:
    from_openalex = meta("Agents That Review Code", arxiv_id="2601.21379")
    from_arxiv = meta(
        "Agents That Review Code",
        source="arxiv",
        source_id="2601.21379",
        arxiv_id="2601.21379",
    )
    assert len(deduplicate([from_openalex, from_arxiv]).papers) == 1


def test_arxiv_id_match_ignores_the_title() -> None:
    a = meta("Original Preprint Title Here", arxiv_id="2601.21379")
    b = meta("A Totally Reworded Published Title", arxiv_id="2601.21379", authors=())
    assert len(deduplicate([a, b]).papers) == 1


def test_the_same_source_record_seen_twice_is_kept_once() -> None:
    a = meta("Agents That Review Code", source_id="W1", doi="10.1000/abc")
    again = meta("Agents That Review Code", source_id="W1", doi="10.1000/abc")
    paper = deduplicate([a, again]).papers[0]
    assert len(paper.records) == 1


def test_different_identifiers_and_titles_stay_apart() -> None:
    a = meta("Agents That Review Code", doi="10.1000/a")
    b = meta("Fuzzing Compilers With Language Models", doi="10.1000/b")
    assert len(deduplicate([a, b]).papers) == 2


def test_a_record_that_bridges_two_groups_joins_them() -> None:
    only_doi = meta("Alpha Beta Gamma Delta", doi="10.1000/x", authors=("A One",))
    only_arxiv = meta(
        "Entirely Other Words Here", arxiv_id="2601.00001", authors=("B Two",)
    )
    both = meta("Whatever", doi="10.1000/x", arxiv_id="2601.00001", authors=())
    dedup = Deduplicator()
    g1 = dedup.add(only_doi)
    g2 = dedup.add(only_arxiv)
    assert g1 != g2
    assert dedup.add(both) == g1  # joined into the older group
    result = dedup.result()
    assert len(result.papers) == 1
    assert len(result.papers[0].records) == 3
    # the dropped group id no longer resolves, later records still find the merged one
    assert dedup.add(meta("Yet Another", arxiv_id="2601.00001")) == g1


# ----------------------------------------------------------------- title matching


@pytest.mark.parametrize(
    ("preprint_title", "published_title"),
    [
        (
            "Human-In-The-Loop Software Development Agents: "
            "Challenges and Future Directions",
            "Human-in-the-loop software development agents - "
            "challenges and future directions",
        ),
        (
            "An Empirical Study of Trust in AI-Generated Code Review",
            "An Empirical Study of Trust in AI Generated Code Reviews",
        ),
        (
            "A Study of Code Review Agents in Industry",
            "The Study of Code Review Agents in Industry",
        ),
    ],
)
def test_preprint_and_published_titles_that_differ_slightly_merge(
    preprint_title: str, published_title: str
) -> None:
    assert title_similarity(preprint_title, published_title) >= MERGE_THRESHOLD
    preprint = meta(preprint_title, source="arxiv", arxiv_id="2501.00001", year=2024)
    published = meta(published_title, doi="10.1109/msr.2025.1", year=2025)
    result = deduplicate([preprint, published])
    assert len(result.papers) == 1
    assert result.papers[0].status == "published"
    assert result.suspects == []


SHORT = "Predicting Developer Acceptance of AI-Generated Code Suggestions"
LONG = SHORT + ": An Empirical Study"


def test_an_added_subtitle_merges_when_the_authors_agree() -> None:
    assert SUSPECT_THRESHOLD <= title_similarity(SHORT, LONG) < MERGE_THRESHOLD
    authors = ("Jing Jiang", "Ana Costa", "Li Hou")
    result = deduplicate([meta(SHORT, authors=authors), meta(LONG, authors=authors)])
    assert len(result.papers) == 1
    assert result.suspects == []


def test_an_added_subtitle_with_most_authors_shared_still_merges() -> None:
    five = ("A One", "B Two", "C Three", "D Four", "E Five")
    result = deduplicate(
        [meta(SHORT, authors=five), meta(LONG, authors=(*five, "F Six"))]
    )
    assert len(result.papers) == 1  # 5 of 6 distinct surnames shared = 0.83


def test_an_added_subtitle_is_only_a_suspect_when_authors_differ() -> None:
    result = deduplicate(
        [
            meta(SHORT, authors=("Jing Jiang", "Ana Costa")),
            meta(LONG, authors=("Jing Jiang", "Maria Rossi")),  # 1 of 3 shared
        ]
    )
    assert len(result.papers) == 2
    assert len(result.suspects) == 1
    suspect = result.suspects[0]
    assert {suspect.paper.title, suspect.other.title} == {SHORT, LONG}
    assert SUSPECT_THRESHOLD <= suspect.similarity < MERGE_THRESHOLD


def test_a_much_larger_author_list_is_not_strong_agreement() -> None:
    four = ("A One", "B Two", "C Three", "D Four")
    result = deduplicate(
        [
            meta(SHORT, authors=four),
            meta(LONG, authors=(*four, "E Five", "F Six")),  # 4 of 6 = 0.67
        ]
    )
    assert len(result.papers) == 2
    assert len(result.suspects) == 1


def test_a_single_shared_author_is_not_strong_agreement() -> None:
    result = deduplicate(
        [meta(SHORT, authors=("Jing Jiang",)), meta(LONG, authors=("Jing Jiang",))]
    )
    assert len(result.papers) == 2
    assert len(result.suspects) == 1


@pytest.mark.parametrize(
    "ids",
    [
        ({"arxiv_id": "2501.00001"}, {"arxiv_id": "2501.00002"}),
        ({"doi": "10.1000/a"}, {"doi": "10.1000/b"}),
    ],
)
def test_agreeing_authors_do_not_override_conflicting_identifiers(
    ids: tuple[dict[str, str], dict[str, str]],
) -> None:
    authors = ("Jing Jiang", "Ana Costa")
    result = deduplicate(
        [meta(SHORT, authors=authors, **ids[0]), meta(LONG, authors=authors, **ids[1])]
    )
    assert len(result.papers) == 2


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Scaling Laws for Code Agents", "Scaling Laws for Code Agents: Part II"),
        (
            "Scaling Laws for Code Agents, Part I",
            "Scaling Laws for Code Agents, Part II",
        ),
        ("Scaling Laws for Code Agents Vol 1", "Scaling Laws for Code Agents Vol 2"),
        ("Llama 2 for Code Review Tasks", "Llama 3 for Code Review Tasks"),
        (
            "Mining Software Repositories ICSE 2024",
            "Mining Software Repositories ICSE 2025",
        ),
    ],
)
def test_titles_that_differ_by_a_number_or_part_never_merge(
    first: str, second: str
) -> None:
    """Same authors, near-identical titles, no identifiers: still different works."""
    authors = ("Jing Jiang", "Ana Costa")
    result = deduplicate([meta(first, authors=authors), meta(second, authors=authors)])
    assert len(result.papers) == 2
    assert result.suspects == []


def test_the_same_numbers_do_not_stop_a_merge() -> None:
    a = meta("GPT-4 Technical Report on Code Review")
    b = meta("GPT-4 technical report on code review!")
    assert len(deduplicate([a, b]).papers) == 1


def test_a_suspect_that_ends_up_merged_anyway_is_not_reported() -> None:
    a = meta(SHORT, authors=("X One", "X Two"), doi="10.1000/p")
    b = meta(LONG, authors=("X One", "Y Two"))  # only a suspect next to a
    c = meta(
        LONG, authors=("X One", "Y Two"), doi="10.1000/p"
    )  # DOI links a, title links b
    result = deduplicate([a, b, c])
    assert len(result.papers) == 1
    assert result.suspects == []


def test_similar_but_different_papers_are_not_merged() -> None:
    # Two real papers by the same authors: "..." (ICSE-SEIP 2024) and
    # "...: Challenges and Future Directions" (MSR 2025).
    first = meta(
        "Human-In-The-Loop Software Development Agents",
        arxiv_id="2411.12924",
        year=2024,
    )
    second = meta(
        "Human-In-The-Loop Software Development Agents: "
        "Challenges and Future Directions",
        arxiv_id="2506.11009",
        year=2025,
    )
    result = deduplicate([first, second])
    assert len(result.papers) == 2
    assert result.suspects == []  # far below the suspect threshold


def test_series_parts_with_different_dois_are_not_merged() -> None:
    part1 = meta("Scaling Laws for Code Agents, Part I", doi="10.1000/p1")
    part2 = meta("Scaling Laws for Code Agents, Part II", doi="10.1000/p2")
    assert title_similarity(part1.title, part2.title) >= MERGE_THRESHOLD
    assert len(deduplicate([part1, part2]).papers) == 2


def test_different_arxiv_ids_block_a_title_merge() -> None:
    a = meta("Scaling Laws for Code Agents, Part I", arxiv_id="2501.00001")
    b = meta("Scaling Laws for Code Agents, Part II", arxiv_id="2501.00002")
    assert len(deduplicate([a, b]).papers) == 2


def test_a_preprint_doi_does_not_block_a_publisher_match() -> None:
    preprint = meta(
        "Agents That Review Code",
        source="arxiv",
        source_id="2501.00001",
        arxiv_id="2501.00001",
        doi="10.48550/arxiv.2501.00001",
    )
    published = meta("Agents That Review Code", doi="10.1109/abc.2025.7")
    assert len(deduplicate([preprint, published]).papers) == 1


def test_years_too_far_apart_block_a_title_merge() -> None:
    a = meta(year=2019)
    b = meta(year=2023)
    assert len(deduplicate([a, b]).papers) == 2


def test_one_year_apart_is_fine_and_unknown_year_does_not_block() -> None:
    assert len(deduplicate([meta(year=2024), meta(year=2025)]).papers) == 1
    assert len(deduplicate([meta(year=2024), meta(year=None)]).papers) == 1


def test_no_common_author_blocks_a_title_merge() -> None:
    a = meta(authors=("Jing Jiang",))
    b = meta(authors=("Maria Rossi",))
    assert len(deduplicate([a, b]).papers) == 2


def test_author_name_styles_still_match() -> None:
    a = meta(authors=("Jing Jiang", "Ana Costa"))
    b = meta(authors=("Costa, A",))
    assert len(deduplicate([a, b]).papers) == 1


def test_records_without_authors_never_merge_by_title() -> None:
    assert len(deduplicate([meta(authors=()), meta(authors=())]).papers) == 2


def test_very_short_titles_never_merge_by_title() -> None:
    assert len(deduplicate([meta("Editorial"), meta("Editorial")]).papers) == 2


def test_title_matching_is_transitive_through_a_group() -> None:
    # c is close to b but not to a; b is close to a. All three are one paper.
    a = meta("Trust in AI Generated Code Review")
    b = meta("Trust in AI-Generated Code Reviews")
    c = meta("Trust in AI-Generated Code Reviews!!")
    assert len(deduplicate([a, b, c]).papers) == 1


# --------------------------------------------------------------------- merging


def test_published_record_is_displayed_and_preprint_info_is_kept() -> None:
    preprint = meta(
        "Agents That Review Code (preprint)",
        source="arxiv",
        source_id="2501.00001",
        arxiv_id="2501.00001",
        abstract="Short abstract.",
        year=2024,
        date="2024-12-30",
        venue=None,
        work_type="preprint",
        citation_count=3,
        links={
            "arxiv": "https://arxiv.org/abs/2501.00001",
            "pdf": "https://arxiv.org/pdf/2501.00001",
        },
        oa_pdf_url="https://arxiv.org/pdf/2501.00001",
        authors=("Jing Jiang",),
    )
    published = meta(
        "Agents That Review Code",
        arxiv_id="2501.00001",
        doi="10.1109/msr.2025.1",
        abstract="A much longer abstract that explains the work in detail.",
        year=2025,
        date="2025-04-28",
        venue="MSR",
        work_type="conference-paper",
        citation_count=9,
        links={
            "doi": "https://doi.org/10.1109/MSR.2025.1",
            "openalex": "https://openalex.org/W1",
        },
        openalex_id="W1",
        authors=("Jing Jiang", "Ana Costa"),
    )
    merged = deduplicate([preprint, published]).papers[0]
    assert merged.title == "Agents That Review Code"
    assert merged.status == "published"
    assert merged.venue == "MSR"
    assert merged.doi == "10.1109/msr.2025.1"
    assert merged.year == 2025
    assert merged.date == "2025-04-28"
    assert merged.arxiv_id == "2501.00001"
    assert merged.openalex_id == "W1"
    assert merged.abstract == "A much longer abstract that explains the work in detail."
    assert merged.authors == ("Jing Jiang", "Ana Costa")
    assert merged.citation_count == 9
    assert merged.work_type == "conference-paper"
    assert merged.links["arxiv"] == "https://arxiv.org/abs/2501.00001"
    assert merged.links["pdf"] == "https://arxiv.org/pdf/2501.00001"
    assert merged.links["openalex"] == "https://openalex.org/W1"
    assert merged.links["doi"] == "https://doi.org/10.1109/msr.2025.1"
    assert merged.oa_pdf_url == "https://arxiv.org/pdf/2501.00001"
    assert merged.records == (preprint, published)  # all sources kept, input order


def test_published_wins_regardless_of_input_order() -> None:
    preprint = meta(
        "Title X Agents", source="arxiv", arxiv_id="2501.00001", work_type="preprint"
    )
    published = meta(
        "Title X Agents", arxiv_id="2501.00001", doi="10.1000/x", venue="ICSE"
    )
    for records in ([preprint, published], [published, preprint]):
        assert merge_records(records).venue == "ICSE"
        assert merge_records(records).status == "published"


def test_publisher_doi_is_preferred_over_the_arxiv_doi() -> None:
    a = meta(doi="10.48550/arxiv.2501.00001", arxiv_id="2501.00001")
    b = meta(
        doi="10.1109/real.2025.9", arxiv_id="2501.00001", work_type="conference-paper"
    )
    assert merge_records([a, b]).doi == "10.1109/real.2025.9"
    assert merge_records([a]).doi == "10.48550/arxiv.2501.00001"


def test_only_preprints_gives_preprint_status() -> None:
    p = meta(
        source="arxiv",
        arxiv_id="2501.00001",
        work_type="preprint",
        venue="arXiv (Cornell University)",
    )
    merged = merge_records([p])
    assert merged.status == "preprint"
    assert merged.venue == "arXiv (Cornell University)"


def test_source_priority_breaks_ties() -> None:
    from_s2 = meta("From S2", source="s2", doi="10.1000/x", venue="V")
    from_openalex = meta("From OpenAlex", doi="10.1000/x", venue="V")
    assert merge_records([from_s2, from_openalex]).title == "From OpenAlex"


def test_merge_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError):
        merge_records([])


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"work_type": "preprint", "doi": "10.1000/x"}, "preprint"),
        ({"source": "arxiv"}, "preprint"),
        ({"doi": "10.48550/arxiv.2501.00001"}, "preprint"),
        ({"doi": "10.1000/x", "venue": "arXiv.org"}, "published"),
        ({"venue": "arXiv.org"}, "preprint"),
        ({"work_type": "article"}, "published"),
        ({"venue": "ICSE"}, "published"),
        ({}, "unknown"),
    ],
)
def test_record_status(kwargs: dict[str, Any], expected: str) -> None:
    assert record_status(meta(**kwargs)) == expected


# ------------------------------------------------------------------- bookkeeping


def test_papers_come_out_in_order_of_first_appearance() -> None:
    a = meta("First Paper About Agents", authors=("A One",), doi="10.1000/1")
    b = meta("Second Paper About Testing", authors=("B Two",), doi="10.1000/2")
    a_again = meta(
        "First Paper About Agents", authors=("A One",), source="s2", doi="10.1000/1"
    )
    result = deduplicate([a, b, a_again])
    assert titles_of(result.papers) == [
        "First Paper About Agents",
        "Second Paper About Testing",
    ]


def test_empty_input() -> None:
    result = deduplicate([])
    assert result.papers == []
    assert result.suspects == []


def test_a_large_batch_is_grouped_correctly() -> None:
    """500 distinct papers, each seen from three sources: 500 groups, 1500 records."""
    batch = []
    for i in range(500):
        title = f"Distinct topic number {i} about code agents and review {i * 7919}"
        authors = (f"Author{i} Surname{i % 40}", f"Other{i} Common")
        batch.append(meta(title, authors=authors, doi=f"10.1000/{i}"))
        batch.append(meta(title, source="s2", authors=authors, doi=f"10.1000/{i}"))
        batch.append(
            meta(
                title.upper(), source="arxiv", authors=authors, arxiv_id=f"2501.{i:05d}"
            )
        )
    result = deduplicate(batch)
    assert len(result.papers) == 500
    assert all(len(p.records) == 3 for p in result.papers)


@pytest.mark.parametrize("arxiv_first", [True, False])
def test_every_source_record_is_kept_whichever_arrives_first(arxiv_first: bool) -> None:
    """Regression: an arXiv record was dropped when an OpenAlex record carrying the
    same arXiv ID had arrived before it."""
    from_openalex = meta("Agents That Review Code", arxiv_id="2601.21379")
    from_arxiv = meta(
        "Agents That Review Code",
        source="arxiv",
        source_id="2601.21379",
        arxiv_id="2601.21379",
    )
    records = (
        [from_arxiv, from_openalex] if arxiv_first else [from_openalex, from_arxiv]
    )
    paper = deduplicate(records).papers[0]
    assert {r.source for r in paper.records} == {"openalex", "arxiv"}
    assert len(paper.records) == 2


def test_an_s2_record_is_kept_next_to_an_openalex_record_with_its_id() -> None:
    from_s2 = meta("Agents That Review Code", source="s2", source_id="abc", s2_id="abc")
    other = meta("Agents That Review Code", s2_id="abc")  # cross-reference by ID
    paper = deduplicate([other, from_s2]).papers[0]
    assert len(paper.records) == 2


def test_a_repository_copy_merges_with_the_published_version() -> None:
    """Regression (real data): MSR paper and its Zenodo copy have different DOIs."""
    title = "Why and When Agentic Pull Requests are (not) Accepted"
    published = meta(title, doi="10.1145/3793302.3793571", venue="MSR")
    zenodo = meta(title, doi="10.5281/zenodo.18008771", work_type="conference-paper")
    merged = deduplicate([zenodo, published]).papers
    assert len(merged) == 1
    assert merged[0].doi == "10.1145/3793302.3793571"  # the publisher DOI is shown
    assert merged[0].venue == "MSR"


@pytest.mark.parametrize(
    "doi", ["10.5281/zenodo.1", "10.2139/ssrn.1", "10.48550/arxiv.2501.00001"]
)
def test_repository_dois_are_not_publisher_dois(doi: str) -> None:
    a = meta("Agents That Review Code", doi=doi)
    b = meta("Agents That Review Code", doi="10.1109/abc.2025.7")
    assert len(deduplicate([a, b]).papers) == 1


def test_two_different_repository_dois_do_not_keep_a_work_apart() -> None:
    a = meta("Agents That Review Code", doi="10.5281/zenodo.1")
    b = meta("Agents That Review Code", doi="10.2139/ssrn.2")
    assert len(deduplicate([a, b]).papers) == 1


def test_a_record_with_a_wrong_title_but_other_ids_stays_apart() -> None:
    """Regression (real data): OpenAlex once returned one paper's title with another
    paper's arXiv ID, year and authors. The title alone must not glue them together."""
    broken = meta(
        "AI-to-AI Code Reviews of GitHub Pull Requests",
        arxiv_id="2212.08073",
        doi="10.4230/lipics.esem.2026.75",
        year=2022,
        authors=("Bai, Yuntao", "Saurav Kadavath"),
    )
    real = meta(
        "AI-to-AI Code Reviews of GitHub Pull Requests",
        arxiv_id="2608.21311",
        doi="10.48550/arxiv.2608.21311",
        year=2026,
        authors=("Niruthiha Selvanayagam", "Taher A. Ghaleb"),
    )
    assert len(deduplicate([broken, real]).papers) == 2


def test_join_callback_reports_kept_and_dropped_groups() -> None:
    joins: list[tuple[int, int]] = []
    dedup = Deduplicator(on_join=lambda kept, dropped: joins.append((kept, dropped)))
    g1 = dedup.add(meta("Alpha Beta Gamma Delta", doi="10.1000/x", authors=("A One",)))
    g2 = dedup.add(
        meta("Other Words Entirely", arxiv_id="2601.00001", authors=("B Two",))
    )
    assert joins == []
    dedup.add(meta("Whatever", doi="10.1000/x", arxiv_id="2601.00001", authors=()))
    assert joins == [(g1, g2)]


def test_group_records_come_back_in_arrival_order_and_merged_works() -> None:
    dedup = Deduplicator()
    first = meta("Agents That Review Code", doi="10.1000/x")
    second = meta("Agents That Review Code", doi="10.1000/x", source="s2", venue="V")
    gid = dedup.add(first)
    assert dedup.add(second) == gid
    assert dedup.group_records(gid) == [first, second]
    assert dedup.merged(gid).venue == "V"
