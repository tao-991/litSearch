"""The candidate list as one self-contained HTML page (no network access needed).

Papers are grouped by the query they fit best, best score first inside a group. A small
script lets the reader filter by score and by text without reloading. Everything that
comes from a data source or a model is escaped, and only http(s) links are used.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from litsearch.pipeline.export import (
    DEFAULT_MAYBE_SCORE,
    DEFAULT_MIN_SCORE,
    NO_RANK,
    Candidate,
    RunSummary,
    group_by_query,
)
from litsearch.pipeline.score import MAX_SCORE

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d232b;--muted:#5b6673;--line:#dde2e8;
--accent:#2457c5;--s5:#16794c;--s4:#1b7f8c;--s3:#b26a00;--s0:#7b8591;--chip:#eef1f5;
--ok:#16794c;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--card:#1c2229;--ink:#e6eaef;
--muted:#9aa5b1;--line:#2c343d;--accent:#7fa7ff;--s5:#4cc38a;--s4:#52c0cf;
--s3:#e0a040;
--s0:#8a94a0;--chip:#252d36;--ok:#4cc38a;--bad:#f2867f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Noto Sans SC",sans-serif}
a{color:var(--accent)}
.top{position:sticky;top:0;z-index:5;background:var(--card);
border-bottom:1px solid var(--line)}
.bar{max-width:980px;margin:0 auto;padding:10px 16px;display:flex;flex-wrap:wrap;
gap:10px 16px;
align-items:center}
.bar strong{margin-right:auto}
.bar select,.bar input{font:inherit;padding:5px 8px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--ink)}
.bar input{min-width:200px}
#count{color:var(--muted);font-size:14px}
main{max-width:980px;margin:0 auto;padding:16px}
h1{margin:8px 0 4px;font-size:26px}
h2{margin:0 0 4px;font-size:20px}
.lead,.muted{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.chip{background:var(--chip);border-radius:999px;padding:3px 12px;font-size:14px}
.note{font-size:14px;color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
vertical-align:top}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
nav.toc{margin:16px 0;padding:12px 16px;background:var(--card);
border:1px solid var(--line);
border-radius:12px}
nav.toc ol{margin:6px 0 0;padding-left:22px}
nav.toc li{margin:3px 0}
section.query{margin:28px 0}
section.query>header{margin-bottom:10px}
.qmeta{color:var(--muted);font-size:14px}
.empty{padding:12px 14px;border:1px dashed var(--line);border-radius:10px;
color:var(--muted)}
article.paper{display:grid;grid-template-columns:44px 1fr;gap:12px;
background:var(--card);
border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0}
article.paper[hidden],tr[hidden],details[hidden]{display:none}
.score{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;
justify-content:center;font-weight:700;font-size:20px;color:#fff;
background:var(--s0)}
.s5 .score{background:var(--s5)}.s4 .score{background:var(--s4)}
.s3 .score{background:var(--s3)}
article.paper h3{margin:0 0 2px;font-size:17px;line-height:1.35}
article.paper p{margin:4px 0}
.meta,.authors,.foot{color:var(--muted);font-size:14px}
.tag{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 8px;
margin:0 6px 4px 0;font-size:13px}
.links a{display:inline-block;margin:2px 8px 0 0;font-size:14px}
.basis{color:var(--muted);font-size:13px}
details.maybe,details.screened{margin:10px 0;border:1px solid var(--line);
border-radius:12px;
background:var(--card);padding:6px 14px}
details>summary{cursor:pointer;padding:6px 0;font-weight:600}
.status{font-size:13px;padding:0 6px;border-radius:6px;background:var(--chip)}
@media (max-width:600px){article.paper{grid-template-columns:1fr}
.score{width:36px;height:36px;font-size:18px}.bar input{min-width:0;width:100%}}
"""

_SCRIPT = """
(function(){
const sel=document.getElementById('minScore'),box=document.getElementById('q'),
count=document.getElementById('count');
function apply(){
  const min=+sel.value,term=box.value.trim().toLowerCase();let shown=0;
  document.querySelectorAll('.paper').forEach(function(el){
    const ok=+el.dataset.score>=min&&(!term||el.dataset.text.indexOf(term)>=0);
    el.hidden=!ok;if(ok)shown++;
  });
  document.querySelectorAll('details.maybe,details.screened').forEach(function(d){
    d.hidden=!d.querySelector('.paper:not([hidden])');
  });
  document.querySelectorAll('section.query').forEach(function(sec){
    const n=sec.querySelectorAll('.paper:not([hidden])').length,
out=sec.querySelector('.shown');
    if(out)out.textContent=n;
  });
  count.textContent=shown+' papers shown';
}
sel.addEventListener('change',apply);box.addEventListener('input',apply);apply();
})();
"""

_STATUS_CLASS = {"ok": "ok"}


def _safe_url(url: str | None) -> str | None:
    """The URL if it is an http(s) link, else ``None`` (never javascript: or data:)."""
    if not url:
        return None
    url = url.strip()
    return url if url.lower().startswith(("http://", "https://")) else None


def _main_link(c: Candidate) -> str | None:
    for url in (
        c.links.get("doi"),
        c.links.get("arxiv"),
        c.links.get("landing_page"),
        c.links.get("openalex"),
        c.oa_pdf_url,
    ):
        if safe := _safe_url(url):
            return safe
    return None


def _link(url: str | None, label: str) -> str:
    safe = _safe_url(url)
    if not safe:
        return ""
    return (
        f'<a href="{escape(safe)}" target="_blank" rel="noopener noreferrer">'
        f"{label}</a>"
    )


def _search_text(c: Candidate) -> str:
    parts = [c.title, c.summary or "", c.reason or "", " ".join(c.tags), c.venue or ""]
    return escape(" ".join(parts).lower(), quote=True)


def _card(c: Candidate, *, show_found_by: bool = True) -> str:
    score = -1 if c.score is None else int(c.score)
    badge = "\u2013" if c.score is None else f"{score:g}"
    title = escape(c.title)
    main = _main_link(c)
    heading = (
        f'<a href="{escape(main)}" target="_blank" rel="noopener noreferrer">'
        f"{title}</a>"
        if main
        else title
    )
    meta = [str(c.year or "year ?"), escape(c.venue or "venue ?")]
    meta_html = " · ".join(meta) + f' · <span class="status">{escape(c.status)}</span>'
    authors = ", ".join(c.authors[:4]) + (" et al." if len(c.authors) > 4 else "")
    rows = [f"<h3>{heading}</h3>", f'<p class="meta">{meta_html}</p>']
    if authors:
        rows.append(f'<p class="authors">{escape(authors)}</p>')
    if c.reason:
        rows.append(f'<p class="fits"><b>Fits</b> {escape(c.reason)}</p>')
    if c.summary:
        basis = (
            "based on the title only, no abstract"
            if c.score_basis == "title"
            else "based on the abstract"
        )
        rows.append(
            f'<p class="summary"><b>Summary</b> <span class="basis">({basis})</span> '
            f"{escape(c.summary)}</p>"
        )
    if c.tags:
        rows.append(
            '<p class="tags">'
            + "".join(f'<span class="tag">{escape(t)}</span>' for t in c.tags)
            + "</p>"
        )
    links = "".join(
        [
            _link(c.links.get("doi"), "DOI"),
            _link(c.links.get("arxiv"), "arXiv"),
            _link(c.oa_pdf_url, "PDF"),
            _link(c.links.get("landing_page"), "page"),
            _link(c.links.get("openalex"), "OpenAlex"),
        ]
    )
    if links:
        rows.append(f'<p class="links">{links}</p>')
    if show_found_by:
        noun = "query" if len(c.queries) == 1 else "queries"
        found = f"Found by {escape(', '.join(c.sources))} · {len(c.queries)} {noun}"
        if c.best_rank < NO_RANK:
            found += f" · best rank {c.best_rank}"
        rows.append(f'<p class="foot">{found}</p>')
    return (
        f'<article class="paper s{max(score, 0)}" data-score="{max(score, 0)}" '
        f'data-text="{_search_text(c)}">'
        f'<div class="score" title="score {badge} of {MAX_SCORE}">{badge}</div>'
        f'<div class="body">{"".join(rows)}</div></article>'
    )


def _sources_table(summary: RunSummary) -> str:
    rows = []
    for s in summary.sources:
        cls = _STATUS_CLASS.get(s.status, "bad")
        rows.append(
            f'<tr><td>{escape(s.source)}</td><td class="{cls}">{escape(s.status)}</td>'
            f"<td>{s.n_requests}</td><td>{s.n_results}</td>"
            f"<td>{escape(s.error or '')}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Source</th><th>Status</th><th>Requests</th>"
        "<th>Results</th><th>Note</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_html(
    candidates: list[Candidate],
    summary: RunSummary,
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    maybe_score: int = DEFAULT_MAYBE_SCORE,
) -> str:
    scored = [c for c in candidates if c.score is not None]
    grouped = group_by_query(
        candidates, summary, min_score=min_score, maybe_score=maybe_score
    )
    n_relevant = sum(len(g.relevant) for g in grouped.groups)
    n_maybe = sum(len(g.maybe) for g in grouped.groups)

    options = {MAX_SCORE: f"{MAX_SCORE} only", min_score: f"{min_score}+ relevant"}
    options[maybe_score] = f"{maybe_score}+ with possibly relevant"
    options[0] = "all, including screened out"
    default_min = maybe_score if scored else 0
    select = "".join(
        f'<option value="{value}"{" selected" if value == default_min else ""}>'
        f"{escape(label)}</option>"
        for value, label in sorted(options.items(), reverse=True)
    )

    out: list[str] = []
    n_queries = summary.n_queries
    chips = [
        f"{n_queries} {'query' if n_queries == 1 else 'queries'}",
        f"{summary.n_papers} papers found",
    ]
    if scored:
        chips += [
            f"{len(scored)} scored",
            f"{n_relevant} relevant ({min_score}+)",
            f"{n_maybe} possibly relevant ({maybe_score})",
        ]
    out.append(f"<h1>{escape(summary.name)}</h1>")
    out.append(
        f'<p class="lead">Status: <b>{escape(summary.status)}</b></p>'
        '<div class="chips">'
        + "".join(f'<span class="chip">{escape(c)}</span>' for c in chips)
        + "</div>"
    )
    if scored:
        out.append(
            f'<p class="note">Scored from 0 to {MAX_SCORE} by '
            f"<code>{escape(summary.scored_by or '')}</code>, relative to the queries "
            "below: a paper is relevant if it addresses at least one of them. Reasons "
            "and summaries are written by a model from the title and abstract only; "
            "links, DOIs, authors and years come from the data sources. Small models "
            "score generously, so read this as a screening aid.</p>"
        )
    else:
        out.append(
            '<p class="note"><b>Not scored.</b> The papers are ordered by how many '
            "queries found them, then by their best rank in a source's results.</p>"
        )
    out.append(
        f"<details><summary>Sources ({len(summary.sources)})</summary>"
        f"{_sources_table(summary)}</details>"
    )

    if scored:
        toc = "".join(
            f'<li><a href="#q{i}">{escape(g.query or "")}</a> '
            f'<span class="muted">· {len(g.relevant)} relevant, '
            f"{len(g.maybe)} possibly relevant, {g.n_found} results</span></li>"
            for i, g in enumerate(grouped.groups, start=1)
        )
        out.append(f'<nav class="toc"><b>Queries</b><ol>{toc}</ol></nav>')
        for i, g in enumerate(grouped.groups, start=1):
            shown_total = len(g.relevant) + len(g.maybe)
            out.append(
                f'<section class="query" id="q{i}"><header><h2>{i}. '
                f"{escape(g.query or '')}</h2>"
                f'<div class="qmeta">{g.n_found} results · '
                f"{len(g.relevant)} relevant · {len(g.maybe)} possibly relevant · "
                f'<span class="shown">{shown_total}</span> shown</div></header>'
            )
            if not g.relevant and not g.maybe:
                out.append(
                    '<div class="empty">No paper fits this query best: it may be '
                    "worded badly or the topic may not be covered by these "
                    "sources.</div>"
                )
            out += [_card(c) for c in g.relevant]
            if g.maybe:
                out.append(
                    f'<details class="maybe"><summary>Possibly relevant (score '
                    f"{maybe_score}): {len(g.maybe)}</summary>"
                    + "".join(_card(c) for c in g.maybe)
                    + "</details>"
                )
            out.append("</section>")
        if grouped.unscored:
            out.append(
                f'<p class="note"><b>{len(grouped.unscored)} papers were not '
                "scored</b> (a limit was reached or the model failed). Run "
                f"<code>litsearch score {summary.run_id}</code> to score them; "
                "they are in the CSV.</p>"
            )
        if grouped.screened_out:
            rows = "".join(
                f'<tr class="paper" data-score="{int(c.score or 0)}" '
                f'data-text="{_search_text(c)}"><td>{c.score:g}</td>'
                f"<td>{_title_cell(c)}</td><td>{escape(c.reason or '')}</td></tr>"
                for c in grouped.screened_out
            )
            out.append(
                f'<details class="screened"><summary>Screened out (score below '
                f"{maybe_score} or fitting no query): {len(grouped.screened_out)}"
                "</summary><table><thead><tr><th>Score</th><th>Title</th><th>Reason"
                f"</th></tr></thead><tbody>{rows}</tbody></table></details>"
            )
    else:
        out.append(
            '<section class="query" id="q0"><header><h2>Candidates</h2></header>'
        )
        out += [_card(c) for c in candidates]
        out.append("</section>")

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(summary.name)} - literature search</title>"
        f"<style>{_CSS}</style></head><body>"
        f'<div class="top"><div class="bar"><strong>{escape(summary.name)}</strong>'
        f'<label>Show <select id="minScore">{select}</select></label>'
        '<input id="q" type="search" placeholder="Filter by title, summary or tag">'
        '<span id="count"></span></div></div>'
        f"<main>{''.join(out)}</main><script>{_SCRIPT}</script></body></html>"
    )


def _title_cell(c: Candidate) -> str:
    main = _main_link(c)
    title = escape(c.title)
    return (
        f'<a href="{escape(main)}" target="_blank" rel="noopener noreferrer">'
        f"{title}</a>"
        if main
        else title
    )


def write_html(
    candidates: list[Candidate],
    summary: RunSummary,
    path: Path,
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    maybe_score: int = DEFAULT_MAYBE_SCORE,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_html(candidates, summary, min_score=min_score, maybe_score=maybe_score),
        encoding="utf-8",
    )
