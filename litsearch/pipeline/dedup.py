"""De-duplication of papers coming from several sources (SPEC §5.2).

Two records are the same paper when, in order of strength:

1. they share an identifier (DOI, arXiv ID, a source's own ID); or
2. their normalized titles are at least 0.92 similar, their years differ by at most 1
   (an unknown year does not count against a match), and they share an author
   surname; or their titles are at least 0.85 similar (for example a preprint title
   plus a subtitle) and their author lists agree strongly (see ``AUTHOR_AGREEMENT``),
   with the same year and identifier checks.

Safeguards beyond the SPEC rule: titles shorter than ``MIN_TITLE_CHARS`` never match by
title; titles that differ by a number or a "part / volume" word never match ("Part I" /
"Part II", "Llama 2" / "Llama 3"); and two records are not merged by title when both
carry arXiv IDs that differ or both carry publisher DOIs that differ.
DOIs from preprint servers and repositories (arXiv, Zenodo, SSRN, ...) do not count as
publisher DOIs here.
The remaining pairs with similarity between 0.85 and 0.92 are only reported as suspected
duplicates.

Merging keeps every source record and prefers the formally published record for the
display fields.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from litsearch.models import MergedPaper, PaperMeta
from litsearch.normalize import author_surname, normalize_title

logger = logging.getLogger(__name__)

MERGE_THRESHOLD = 0.92
SUSPECT_THRESHOLD = 0.85
MAX_YEAR_GAP = 1
# Between SUSPECT and MERGE similarity, records are still merged when their author
# lists agree strongly: at least this many authors on both sides and surname overlap
# (Jaccard) of at least AUTHOR_AGREEMENT.
MIN_AGREEING_AUTHORS = 2
AUTHOR_AGREEMENT = 0.8
MIN_TITLE_CHARS = 12
ARXIV_DOI_PREFIX = "10.48550/arxiv."
# A word that appears in only one of two titles and is one of these (or a number) marks
# the works as different: "Part I" vs "Part II", "Llama 2" vs "Llama 3", "ICSE 2024" vs
# "ICSE 2025".
_NUMBERING_WORDS = frozenset(
    "part vol volume chapter section edition ii iii iv vi vii viii ix".split()
)
# DOIs minted by preprint servers and data repositories, not by a publisher. They name a
# copy of the work, so two different ones never prove that two works are distinct.
REPOSITORY_DOI_PREFIXES = (
    "10.48550/",  # arXiv
    "10.5281/",  # Zenodo
    "10.2139/",  # SSRN
    "10.31219/",  # OSF Preprints
    "10.17605/",  # OSF
    "10.31234/",  # PsyArXiv
    "10.31235/",  # SocArXiv
    "10.36227/",  # TechRxiv
    "10.20944/",  # Preprints.org
    "10.21203/",  # Research Square
    "10.22541/",  # Authorea
    "10.6084/",  # figshare
)

# Which record to show first when the same paper comes from several sources.
SOURCE_PRIORITY = {"openalex": 0, "s2": 1, "arxiv": 2}
_PUBLISHED_TYPES = frozenset(
    {
        "article",
        "journal-article",
        "conference-paper",
        "proceedings-article",
        "book-chapter",
        "book",
        "review",
        "letter",
        "editorial",
        "dissertation",
        "report",
    }
)
_STATUS_RANK = {"published": 0, "unknown": 1, "preprint": 2}


# ----------------------------------------------------------------- record helpers


def _is_arxiv_doi(doi: str | None) -> bool:
    return bool(doi) and str(doi).startswith(ARXIV_DOI_PREFIX)


def _is_publisher_doi(doi: str | None) -> bool:
    return bool(doi) and not str(doi).startswith(REPOSITORY_DOI_PREFIXES)


def record_status(record: PaperMeta) -> str:
    """``"published"``, ``"preprint"`` or ``"unknown"`` for a single source record."""
    publisher_doi = _is_publisher_doi(record.doi)
    if record.work_type == "preprint":
        return "preprint"
    if record.source == "arxiv" or _is_arxiv_doi(record.doi):
        return "preprint"
    if publisher_doi:
        return "published"
    if record.venue and record.venue.lower().startswith("arxiv"):
        return "preprint"
    if record.work_type in _PUBLISHED_TYPES or record.venue:
        return "published"
    return "unknown"


def _record_key(paper: PaperMeta) -> str:
    """Identity of one source record (kept apart from the identifier keys below)."""
    return f"record:{paper.source}:{paper.source_id}"


def _identifier_keys(paper: PaperMeta) -> set[str]:
    keys = {_record_key(paper)}
    if paper.doi:
        keys.add(f"doi:{paper.doi}")
    if paper.arxiv_id:
        keys.add(f"arxiv:{paper.arxiv_id}")
    if paper.openalex_id:
        keys.add(f"openalex:{paper.openalex_id}")
    if paper.s2_id:
        keys.add(f"s2:{paper.s2_id}")
    return keys


def _surnames(paper: PaperMeta) -> set[str]:
    return {s for name in paper.authors if (s := author_surname(name))}


def _publisher_dois(paper: PaperMeta) -> set[str]:
    return {paper.doi} if paper.doi and _is_publisher_doi(paper.doi) else set()


def _authors_agree(surnames: set[str], other: PaperMeta) -> bool:
    """True when both author lists are long enough and nearly the same people."""
    theirs = _surnames(other)
    if min(len(surnames), len(theirs)) < MIN_AGREEING_AUTHORS:
        return False
    return len(surnames & theirs) / len(surnames | theirs) >= AUTHOR_AGREEMENT


def _years_compatible(year: int | None, years: set[int]) -> bool:
    if year is None or not years:
        return True  # an unknown year cannot contradict a match
    return any(abs(year - other) <= MAX_YEAR_GAP for other in years)


# ------------------------------------------------------------------ title matching


def title_similarity(a: str, b: str) -> float:
    """Similarity (0..1) of two titles after normalization."""
    return _similarity(normalize_title(a), normalize_title(b))


def _numbered_differently(a: str, b: str) -> bool:
    """True when the titles differ by a number or a "part / volume" style word."""
    return any(
        word.isdigit() or word in _NUMBERING_WORDS
        for word in set(a.split()) ^ set(b.split())
    )


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return matcher.ratio()


def _similarity_if_close(a: str, b: str) -> float:
    """Like ``_similarity`` but gives 0.0 early if the suspect level is unreachable."""
    if not a or not b:
        return 0.0
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    if (
        matcher.real_quick_ratio() < SUSPECT_THRESHOLD
        or matcher.quick_ratio() < SUSPECT_THRESHOLD
    ):
        return 0.0
    return matcher.ratio()


# ----------------------------------------------------------------------- merging


def merge_records(records: Sequence[PaperMeta]) -> MergedPaper:
    """Combine the source records of one paper.

    Display fields come from the best record: published before unknown before
    preprint, then OpenAlex, Semantic Scholar, arXiv, then input order. The longest
    abstract and the highest citation count win; every record is kept in ``records``.
    """
    if not records:
        raise ValueError("cannot merge an empty list of records")
    ordered = [
        r
        for _, r in sorted(
            enumerate(records),
            key=lambda ir: (
                _STATUS_RANK[record_status(ir[1])],
                SOURCE_PRIORITY.get(ir[1].source, len(SOURCE_PRIORITY)),
                ir[0],
            ),
        )
    ]
    primary = ordered[0]

    def first(getter):  # type: ignore[no-untyped-def]
        return next((v for r in ordered if (v := getter(r)) is not None), None)

    abstracts = [
        r.abstract.strip() for r in ordered if r.abstract and r.abstract.strip()
    ]
    dated = next((r for r in ordered if r.year is not None), None)
    year = dated.year if dated else None
    date = dated.date if dated else first(lambda r: r.date)

    doi = next(
        (r.doi for r in ordered if r.doi and _is_publisher_doi(r.doi)),
        first(lambda r: r.doi),
    )
    venue = next(
        (r.venue for r in ordered if r.venue and record_status(r) != "preprint"),
        first(lambda r: r.venue),
    )
    citation_counts = [
        r.citation_count for r in ordered if r.citation_count is not None
    ]

    links: dict[str, str] = {}
    for record in ordered:
        for key, url in record.links.items():
            links.setdefault(key, url)
    if doi:
        links["doi"] = f"https://doi.org/{doi}"

    statuses = {record_status(r) for r in ordered}
    status = (
        "published"
        if "published" in statuses
        else "preprint"
        if "preprint" in statuses
        else "unknown"
    )
    return MergedPaper(
        title=primary.title,
        abstract=max(abstracts, key=len) if abstracts else None,
        authors=next((r.authors for r in ordered if r.authors), ()),
        year=year,
        date=date,
        venue=venue,
        doi=doi,
        arxiv_id=first(lambda r: r.arxiv_id),
        openalex_id=first(lambda r: r.openalex_id),
        s2_id=first(lambda r: r.s2_id),
        citation_count=max(citation_counts) if citation_counts else None,
        links=links,
        oa_pdf_url=first(lambda r: r.oa_pdf_url),
        work_type=first(lambda r: r.work_type),
        status=status,
        records=tuple(records),
    )


# ------------------------------------------------------------------ the grouping


@dataclass(frozen=True, slots=True)
class SuspectedDuplicate:
    """Two records that look alike (0.85 <= similarity < 0.92) but were not merged."""

    paper: PaperMeta
    other: PaperMeta
    similarity: float


@dataclass(frozen=True, slots=True)
class DedupResult:
    papers: list[MergedPaper]
    suspects: list[SuspectedDuplicate]


@dataclass
class _Group:
    gid: int
    records: list[tuple[int, PaperMeta]] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)
    years: set[int] = field(default_factory=set)
    surnames: set[str] = field(default_factory=set)
    publisher_dois: set[str] = field(default_factory=set)
    arxiv_ids: set[str] = field(default_factory=set)


class Deduplicator:
    """Groups records of the same paper as they arrive.

    ``add`` returns the id of the group the record ended up in. A record that matches
    two existing groups joins them into one. ``result()`` produces the merged papers in
    order of first appearance.
    """

    def __init__(self, on_join: Callable[[int, int], None] | None = None) -> None:
        # on_join(kept_gid, dropped_gid) is called whenever two groups become one
        self._on_join = on_join
        self._groups: dict[int, _Group] = {}
        self._by_key: dict[str, int] = {}
        self._by_surname: defaultdict[str, set[int]] = defaultdict(set)
        self._next_gid = 0
        self._seq = 0
        self._suspects: list[tuple[PaperMeta, PaperMeta, float]] = []

    def add(self, paper: PaperMeta) -> int:
        seq = self._seq
        self._seq += 1
        keys = _identifier_keys(paper)
        matched = {self._by_key[k] for k in keys if k in self._by_key}
        matched |= self._match_by_title(paper, exclude=matched)
        if matched:
            group = self._join(sorted(matched))
        else:
            group = _Group(gid=self._next_gid)
            self._next_gid += 1
            self._groups[group.gid] = group
        self._attach(group, seq, paper, keys)
        return group.gid

    def _match_by_title(self, paper: PaperMeta, exclude: set[int]) -> set[int]:
        title = normalize_title(paper.title)
        surnames = _surnames(paper)
        if len(title) < MIN_TITLE_CHARS or not surnames:
            return set()
        candidates: set[int] = set()
        for surname in surnames:
            candidates |= self._by_surname.get(surname, set())
        publisher_dois = _publisher_dois(paper)
        arxiv_ids = {paper.arxiv_id} if paper.arxiv_id else set()

        found: set[int] = set()
        for gid in sorted(candidates - exclude):
            group = self._groups[gid]
            if not _years_compatible(paper.year, group.years):
                continue
            if (
                publisher_dois
                and group.publisher_dois
                and group.publisher_dois.isdisjoint(publisher_dois)
            ):
                continue  # different publisher DOIs: distinct works
            if arxiv_ids and group.arxiv_ids and group.arxiv_ids.isdisjoint(arxiv_ids):
                continue  # different arXiv IDs: distinct works
            similarity = max(
                (
                    _similarity_if_close(title, t)
                    for t in group.titles
                    if not _numbered_differently(title, t)
                ),
                default=0.0,
            )
            if similarity >= MERGE_THRESHOLD:
                found.add(gid)
            elif similarity >= SUSPECT_THRESHOLD:
                if any(_authors_agree(surnames, r) for _, r in group.records):
                    found.add(gid)
                else:
                    other = group.records[0][1]
                    self._suspects.append((paper, other, similarity))
        return found

    def _join(self, gids: list[int]) -> _Group:
        root = self._groups[gids[0]]
        for gid in gids[1:]:
            other = self._groups.pop(gid)
            root.records.extend(other.records)
            root.keys |= other.keys
            root.titles |= other.titles
            root.years |= other.years
            root.publisher_dois |= other.publisher_dois
            root.arxiv_ids |= other.arxiv_ids
            root.surnames |= other.surnames
            for key in other.keys:
                self._by_key[key] = root.gid
            for surname in other.surnames:
                self._by_surname[surname].discard(gid)
                self._by_surname[surname].add(root.gid)
            if self._on_join is not None:
                self._on_join(root.gid, gid)
        return root

    def _attach(
        self, group: _Group, seq: int, paper: PaperMeta, keys: set[str]
    ) -> None:
        if _record_key(paper) in group.keys:
            return  # the same source record seen again (e.g. by another query)
        group.records.append((seq, paper))
        group.keys |= keys
        for key in keys:
            self._by_key[key] = group.gid
        if title := normalize_title(paper.title):
            group.titles.add(title)
        if paper.year is not None:
            group.years.add(paper.year)
        group.publisher_dois |= _publisher_dois(paper)
        if paper.arxiv_id:
            group.arxiv_ids.add(paper.arxiv_id)
        for surname in _surnames(paper):
            group.surnames.add(surname)
            self._by_surname[surname].add(group.gid)

    def group_records(self, gid: int) -> list[PaperMeta]:
        """The source records of a group, in the order they arrived."""
        group = self._groups[gid]
        return [r for _, r in sorted(group.records, key=lambda sr: sr[0])]

    def merged(self, gid: int) -> MergedPaper:
        return merge_records(self.group_records(gid))

    def result(self) -> DedupResult:
        group_of = {
            (r.source, r.source_id): gid
            for gid, g in self._groups.items()
            for _, r in g.records
        }
        suspects = [
            SuspectedDuplicate(a, b, round(sim, 3))
            for a, b, sim in self._suspects
            if group_of.get((a.source, a.source_id))
            != group_of.get((b.source, b.source_id))
        ]
        for s in suspects:
            logger.info(
                "suspected duplicate (%.2f): %r vs %r",
                s.similarity,
                s.paper.title,
                s.other.title,
            )
        papers = [
            merge_records([r for _, r in sorted(g.records, key=lambda sr: sr[0])])
            for _, g in sorted(self._groups.items())
        ]
        return DedupResult(papers=papers, suspects=suspects)


def deduplicate(papers: Iterable[PaperMeta]) -> DedupResult:
    """Merge records of the same paper; see the module docstring for the rules."""
    dedup = Deduplicator()
    for paper in papers:
        dedup.add(paper)
    return dedup.result()
