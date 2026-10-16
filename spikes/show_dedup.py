"""Show what the de-duplicator does with the saved Phase 0 search results.

    uv run python spikes/show_dedup.py   ->  spikes/samples/dedup.md

Reads the raw responses in spikes/samples/ (run the Phase 0 spikes first), turns them
into PaperMeta with the real collectors' parsers and de-duplicates them. No network.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from litsearch.collectors.arxiv import parse_feed
from litsearch.collectors.openalex import parse_work
from litsearch.collectors.semantic_scholar import parse_paper
from litsearch.models import MergedPaper, PaperMeta
from litsearch.pipeline.dedup import deduplicate

SAMPLES = Path(__file__).parent / "samples"


def load_records() -> list[PaperMeta]:
    records: list[PaperMeta] = []
    for q in ("q1", "q2", "q3"):
        oa = json.loads((SAMPLES / f"openalex_search_{q}.json").read_text())
        records += [p for w in oa["results"] if (p := parse_work(w))]
        s2_file = SAMPLES / f"s2_search_{q}.json"
        if s2_file.exists() and s2_file.stat().st_size > 5:
            data = json.loads(s2_file.read_text()).get("data", [])
            records += [p for d in data if (p := parse_paper(d))]
        records += parse_feed((SAMPLES / f"arxiv_search_{q}.xml").read_text())[1]
    return records


def esc(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def describe(p: MergedPaper) -> list[str]:
    ids = [
        f"DOI {p.doi}" if p.doi else "no DOI",
        f"arXiv {p.arxiv_id}" if p.arxiv_id else "no arXiv ID",
    ]
    lines = [
        f"### {p.title}",
        f"**{p.status}** · {p.year or 'year ?'} · {p.venue or 'venue ?'} · {' · '.join(ids)}",
        "",
        "| Source | Source ID | Title as returned by the source | Year | Venue |",
        "|---|---|---|---|---|",
    ]
    for r in p.records:
        lines.append(
            f"| {r.source} | {r.source_id} | {esc(r.title)} | {r.year or ''} | {esc(r.venue)} |"
        )
    return [*lines, ""]


def main() -> None:
    records = load_records()
    result = deduplicate(records)
    n_sources = Counter(len({r.source for r in p.records}) for p in result.papers)
    status = Counter(p.status for p in result.papers)
    multi = sorted(
        (p for p in result.papers if len(p.records) > 1),
        key=lambda p: (-len({r.source for r in p.records}), p.title),
    )
    single = [p for p in result.papers if len(p.records) == 1]

    out = [
        "# De-duplication of the Phase 0 search results",
        "",
        f"- Records fetched: **{len(records)}** "
        f"({', '.join(f'{k}: {v}' for k, v in Counter(r.source for r in records).items())})",
        f"- Distinct papers after de-duplication: **{len(result.papers)}**",
        f"- Seen in 1 / 2 / 3 sources: **{n_sources[1]} / {n_sources[2]} / {n_sources[3]}**",
        f"- Status: {', '.join(f'{k}: {v}' for k, v in sorted(status.items()))}",
        f"- Suspected duplicates (not merged): **{len(result.suspects)}**",
        "",
        "## Papers merged from several records",
        "",
    ]
    for p in multi:
        out += describe(p)

    out += ["## Suspected duplicates (looks alike, not merged)", ""]
    if result.suspects:
        out += [
            "| Similarity | Title A | Source A | Title B | Source B |",
            "|---|---|---|---|---|",
        ]
        for s in result.suspects:
            out.append(
                f"| {s.similarity} | {esc(s.paper.title)} | {s.paper.source} "
                f"| {esc(s.other.title)} | {s.other.source} |"
            )
    else:
        out.append("None.")
    out += ["", "## Papers found by one record only", ""]
    out += [
        f"- {p.title} ({p.year or '?'}) — {p.status}, {p.records[0].source}"
        for p in single
    ]

    target = SAMPLES / "dedup.md"
    target.write_text("\n".join(out) + "\n")
    print(target)
    print(
        f"{len(records)} records -> {len(result.papers)} papers; "
        f"{len(multi)} merged, {len(result.suspects)} suspects"
    )


if __name__ == "__main__":
    main()
