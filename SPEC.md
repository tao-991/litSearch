# Lit Search — Specification (SPEC v0.3)

> Implementation notes for Claude Code. v0.3 supersedes v0.2 and folds in the Phase 0 findings (`docs/spike-findings.md`). Version 1 does only "deep retrieval + funnel screening + abstract-based summaries and links"; scheduled monitoring, Q&A and review writing come later. Decisions already made are to be followed as written; items marked "to verify" must be confirmed against official docs or real requests before code is written.
>
> A Chinese copy of the earlier v0.2 is kept in `docs/zh-CN/SPEC.md` for reference only; this English file is authoritative.

## 0. How to work (for Claude Code)

1. Work in phases (§11). Before each phase, write a short plan for the user to confirm; after it, run that phase's acceptance checklist, then commit.
2. Do not write external API field names, limits or model IDs from memory. Check official docs or use real requests, and record conclusions in `docs/spike-findings.md`.
3. Do not add heavy dependencies not listed here (vector stores, Celery, Redis, Postgres, ...). Explain the reason before adding any dependency.
4. Unit tests must not touch the real network; the real network is only for `spikes/` and manual acceptance.
5. Every tunable parameter goes through an environment variable or CLI flag; keep `.env.example` current; never hard-code model IDs, e-mail addresses or keys.
6. See §1.2 for what is out of scope.
7. Everything committed to the repository (docs, code comments, CLI messages, commit messages) is written in English.

## 1. Background and goals

### 1.1 Goals

Pain point: asking an AI to do literature search in a chat gets rate-limited, returns incomplete results, stops after two or three rounds, and gives no visibility into coverage.

Version 1 should: given a "project description + keywords + optional seed papers", **run multi-round, multi-source, traceable deep retrieval automatically**, screen results through a "human reading funnel", and produce a ranked report. Each paper gets an abstract-based summary, a relevance explanation and links. The run also records which queries were used, whether each source succeeded, and how many new papers each round added, so coverage is **visible and auditable**.

### 1.2 Non-goals (version 1)

- Scheduled monitoring, web UI, cross-paper Q&A, review writing, full-text PDF reading (see §7, later phases).
- Vector stores / full-text chunked RAG.
- Bypassing paywalls; scraping publisher sites; any gray-area acquisition channel.

### 1.3 Scale and profiles

The tool searches broadly so that recall is high, but a person only ever reads the short ranked list at the top of the report (the first 30 or so papers, plus a collapsed "possibly relevant" section). Single user, command line.

Three profiles set the defaults of the size-related options (`--profile quick|standard|deep`); every value can still be overridden. **The default profile is `quick`** (decision 2026-10-16). The numbers are initial values, to be tuned during Phase 1 acceptance:

| Profile | Queries | Papers per query and source | Snowball rounds | Model reads at most | Time limit |
|---|---|---|---|---|---|
| `quick` (default) | about 8 | 15 | 1 | 150 papers | 10 min |
| `standard` | about 20 | 30 | 2 | 500 papers | 30 min |
| `deep` | about 40 | 50 | 3 | 1500 papers | 60 min |

## 2. Reading funnel (mimicking how people read)

| Level | What is read | What is done | Version 1 |
|---|---|---|---|
| L0 | Metadata | Retrieval / recall | yes |
| L1 | Title + abstract | Relevance score, 3-sentence summary, topic tags (batched, cheap model) | yes |
| L2 | Abstract + introduction + conclusion | Skim the top K papers that passed L1; needs the PDF | later (Phase 2) |
| L3 | Full text | Close reading of a few papers the user picks by hand | later |

Principle: **the lower the level, the more expensive it is, so only papers that pass the previous level move on.** Version 1 stops at L1, but the data model must leave room for L2/L3 (paper state, PDF link fields).

## 3. Retrieval strategy (what determines "depth")

### 3.1 Input

- `project.md`: free text describing the research question, methods, data, angles of interest, known related work.
- Optional `keywords`, `seeds` (list of DOIs / arXiv IDs / titles), date range, field filters.
- Optional `known.txt`: papers the user already knows to be relevant (used to evaluate recall, see §11).

- **Free-form input (decided 2026-10-16, implemented in Phase 1e).** The user does not have to write queries. They can describe the research interest in plain language and/or hand over one or more PDFs ("I am interested in this paper, read it and find related work"). For a PDF the model reads the title, abstract and introduction (Claude's native PDF support, see §7), writes a short "what this is about" description that is **shown to the user for editing and confirmation**, and the PDF is also added to the seed papers for snowballing. Reading only the opening of a paper to understand its topic is not the full-text reading that §1.2 excludes. Until Phase 1e, queries are given by hand with `--query "..."` (repeatable) or `--queries-file` (one query per line, `#` for comments).

### 3.2 Query generation

The model generates N diverse queries from the input (synonyms, method names, different phrasings of the question, terms from adjacent fields) and stores them in the database; they are also exported to `queries.yaml`, which the user can edit and re-run. Each query records its hit count per source and its "effective hit count" (score at or above the threshold).

Phase 0 findings that shape query generation:

- Queries must be **short, precise phrases**. OpenAlex's default `search` matches full text, so long sentences return tens of thousands of weakly related papers.
- Each source has its own **query-syntax adapter** (for example arXiv needs boolean syntax such as `all:"coding agent" AND all:"pull request"`; the S2 bulk endpoint is boolean as well). The arXiv adapter implemented in Phase 1b splits a phrase into keywords that must all match (stopwords dropped, hyphenated words and quoted phrases kept as phrases); whether this is too strict is to be judged with the recall check.
- Recall depends heavily on wording: a known relevant paper was missed by three hand-written queries and found at rank 2 once the query used the paper's own terms. This is why query diversity, snowballing and the `known.txt` recall check all matter.

### 3.3 Multi-source, paginated recall

For each query, each source is paged up to a cap (`--per-query`; the default comes from the profile, see §1.3), not just the first page. OpenAlex returns up to 200 results per request at the same cost, so it is asked for the whole cap in one request. Concurrency per source is 1 (requests to one source are serial); different sources may run concurrently.

### 3.4 Merge and de-duplication

Rules in §5.2. Only de-duplicated papers go to scoring, so the same paper is never paid for twice.

### 3.5 L1 scoring and summarization

- Send title + abstract in batches (about 10–20 papers per call, configurable) and require JSON output: `{id, score 0-10, reason, summary (3 sentences), tags[]}`.
- **Summaries may only be based on the supplied title and abstract**; the report states "abstract-based" explicitly.
- Lenient threshold: scores in the `maybe` band (configurable, e.g. 4–5) go to a "possibly relevant" bucket rather than being dropped.
- Cache key: (paper_id, project_hash, prompt_version, model).
- Papers without an abstract fall back to another source's abstract; if none exists they are scored on title only and flagged "no abstract". Google Scholar snippets are **not** abstracts and must not be used for scoring (see §4).

### 3.6 Citation snowballing

Take the top M papers by L1 score as seeds and fetch their references and citing papers (OpenAlex is the primary source, Semantic Scholar secondary) as new candidates, then return to 3.4–3.5; repeat for R rounds (`--rounds`; the default comes from the profile, see §1.3). User-supplied `seeds` are included among the seeds.

### 3.7 Saturation and stopping

Each round counts "new papers at or above the threshold". Stop when any condition holds: the round limit is reached; the number of new effective papers is below `--saturation` (default 3); a budget limit is hit. The report plots new papers per round so that "is it deep enough" is visible.

### 3.8 Budget and confirmation

- `--dry-run`: prints the plan and the estimate (requests per source, expected duration, upper bound on results, **expected OpenAlex credits** next to the remaining quota read from the response headers of a free single-work lookup) and stops. It fetches no search results, stores nothing and **does not call the model**. With `--resume` it shows only the work still to do.
- Before a real run, show the same estimate and ask for confirmation (`--yes` skips it).
- Limits: `--max-scored` (maximum papers to score), `--max-llm-calls`, `--max-minutes` (wall-clock limit: when it is reached the run stops cleanly, saves what it has and still writes the report). When more candidates exist than `--max-scored` allows, the best-ranked ones (by position in the source result lists, interleaved across queries and sources) are scored first.
- Every step is written to the database; `--resume RUN_ID` continues an interrupted run.

## 4. Data sources and collectors

Field names and limits below were verified in Phase 0 (2026-10-16) unless marked "to verify". Details and evidence are in `docs/spike-findings.md`.

Common interface:

```python
class Collector(Protocol):
    name: str
    async def search(self, query: str, *, limit: int, offset: int|cursor, since: date|None) -> Page[PaperMeta]: ...
    async def references(self, paper: PaperMeta, limit: int) -> list[PaperMeta]: ...   # optional
    async def citations(self, paper: PaperMeta, limit: int) -> list[PaperMeta]: ...    # optional
```

Every collector has its own rate limiting, exponential-backoff retry, circuit breaker, and a contact e-mail in the request headers. `PaperMeta` contains at least: title / abstract / authors / year / date / venue / doi / arxiv_id / per-source IDs / links of each kind / open-access info / source name.

- **OpenAlex** (primary source):
  - `/works` search and filters; references via `referenced_works` plus `filter=openalex:W1|W2|...`; citing papers via `filter=cites:W...`.
  - Abstracts come as `abstract_inverted_index` and are reconstructed by ordering words by position.
  - **Quota model**: without an API key, every network egress IP shares 1000 credits per day ($0.10). A request with a search term costs 10 credits; a pure filter request (references, citations, batch by ID) costs 1; a single-entity lookup and error responses cost 0. The `mailto` parameter does **not** raise the quota (it is only an identifier). A free API key raises it tenfold ($1/day). A request costs the same 10 credits whether it asks for 20 or 200 results (measured), so fetch large pages (up to 200) rather than many small ones.
  - Paging: use `page` (at most 10,000 records; `per-page` between 1 and 200). `cursor` paging past the first page requires an API key (401 otherwise).
  - Two search modes: the API's plain `search` is a full-text relevance search (284,120 matches for one test phrase, noisy), while `filter=title_and_abstract.search:` matches titles and abstracts only (343 matches for the same phrase, which can be fetched completely with two requests). **Decision (2026-10-16): title-and-abstract is the default mode; full-text is an optional supplementary mode** selected with a command-line option. Both cost 10 credits per request.
  - A free key is recommended but optional; the collector must work without one and report quota exhaustion in `source_runs`.
- **Semantic Scholar**: Graph API search, references, citations; optional API key. **Best-effort source**: unauthenticated requests share a quota with all other anonymous users and about 85% of Phase 0 requests returned 429 (intermittent, independent of our request rate). Treat it as optional: failures go to `source_runs` and the report, and snowballing must not depend on it. Search paging is `offset`/`limit` with `offset + limit < 1000`; use `/paper/search/bulk` beyond that. The references/citations endpoints are still **to verify**.
- **arXiv**: official API (Atom XML), at least 3 seconds between requests, one request at a time (confirmed: 12 requests at 3 s intervals, no 429). No citation data. IDs come as `http://arxiv.org/abs/2601.21379v1` and must be normalized (§5.2). Only a minority of preprints carry `doi`/`journal_ref`, so "published or not" cannot be decided from arXiv alone. Date filtering via `submittedDate` is still **to verify**.
- **Google Scholar** (`--gs`, off by default): `scholarly` run in `asyncio.to_thread`; install with `bibtexparser<2` (version 2 breaks `scholarly`'s import).
  - Used only as a **supplementary round**: for the few queries with the highest effective hit count, fetch 1–2 pages each, with random delays (`GS_MIN_DELAY`/`GS_MAX_DELAY`), proxy support; on a captcha/ban stop GS immediately, record `blocked`, **never retry in a storm**, and let the other sources continue.
  - Phase 0 showed it works at low frequency (60 results, no block), but results are search snippets: no DOI or arXiv ID, abbreviated author names, truncated venues, a snippet instead of an abstract (about 190 characters), and `eprint_url` is always `javascript:void(0)`. Only `pub_url` is usable.
  - Therefore GS results must be **matched back to OpenAlex** by title (and DOI when possible) to obtain a DOI, full abstract and links. Unmatched results are flagged `gs_only` and are not abstract-scored.
  - Citation graphs use OpenAlex / S2, never GS.

RRI (`nhdandz/RRI`, MIT) `src/collectors/base.py` (RateLimiter, CircuitBreaker, tenacity retry) may be used as a reference for style only; **do not fork it**. If code is copied directly, keep its copyright notice.

## 5. Data model and de-duplication

### 5.1 SQLite tables (as implemented in `litsearch/db.py`, schema version 1)

- **runs**: id, project_hash, params (JSON), status (`running`/`completed`/`failed`/`interrupted`), started_at, finished_at.
- **queries**: id, run_id, text, origin (`llm`/`user`); unique per (run, text). Hit counts and "effective hit" counts per query and source are **derived** from `hits` and `scores` rather than stored.
- **papers**: id, title, abstract, authors (JSON), year, date, venue, doi, arxiv_id, openalex_id, s2_id, citation_count, links (JSON), oa_pdf_url, state (`candidate`/`scored`/`dismissed`/...). `doi` and `arxiv_id` hold the normalized values of §5.2; each ID column is unique when present, so the database itself rejects duplicates. Papers are shared across runs.
- **paper_records**: id, paper_id, source, source_id, raw (JSON of the source's record), fetched_at; unique per (source, source_id). Keeps every version and source of a merged paper.
- **hits**: id, run_id, query_id (NULL for papers found by snowballing), source, paper_id, rank, round.
- **scores** (the LLM result cache): paper_id, project_hash, input_hash (hash of the title and abstract that were scored), prompt_version, model, score, reason, summary, tags (JSON); the primary key is (paper_id, project_hash, input_hash, prompt_version, model).
- **edges**: src_paper_id, dst_paper_id, kind (`cites`).
- **source_runs**: id, run_id, source, status (`ok`/`blocked`/`rate_limited`/`error`/`skipped`), n_requests, n_results, error; unique per (run, source). **This is the key to "failures are visible" and must appear in the report.**

### 5.2 De-duplication rules

Normalization first:

- DOI: strip the `https://doi.org/` prefix and lowercase. A preprint DOI of the form `10.48550/arxiv.<id>` also yields the arXiv ID.
- arXiv ID: strip the `http://arxiv.org/abs/` prefix and the version suffix (`v1`, `v7`).
- A DOI lookup may return a record whose stored DOI differs from the one queried (observed in OpenAlex), so match on several keys rather than trusting a single returned DOI.

Then (implemented in `litsearch/pipeline/dedup.py`):

1. Exact match on a normalized identifier: DOI, arXiv ID (version stripped), or a source's own ID.
2. Otherwise, title match, in two bands:
   - normalized-title similarity ≥ 0.92 (Python's `difflib.SequenceMatcher` ratio on lowercase, punctuation-free, accent-free titles), **and** years differ by ≤ 1 (an unknown year does not count against a match), **and** at least one shared author surname (`Last, First`, `First Last` and `F Last` styles are all reduced to the surname);
   - similarity from 0.85 up to 0.92 (typically a preprint title plus a subtitle): merged only if the author lists agree strongly, meaning both have at least 2 authors and the surname overlap (Jaccard) is at least 0.8, together with the same year and identifier checks. Otherwise the pair is only logged and reported as a "suspected duplicate".

Safeguards added during implementation (a title match is refused when):

- either title is shorter than 12 normalized characters, or either record has no authors;
- the titles differ by a number or a "part / volume" style word ("Part I" vs "Part II", "Llama 2" vs "Llama 3", "ICSE 2024" vs "ICSE 2025");
- both records carry arXiv IDs and they differ, or both carry **publisher** DOIs and they differ. DOIs minted by preprint servers and repositories (arXiv, Zenodo, SSRN, OSF, TechRxiv, ...) do not count as publisher DOIs, because they name a copy of the work, not a different work.

Known limitation: if a published version adds a subtitle to the preprint title, there is no shared identifier, and the author lists do not agree strongly (different author order is fine, but added or dropped authors lower the overlap), the pair is only reported as a suspect. In practice OpenAlex usually links the arXiv copy to the published work, so they share the arXiv ID.

When merging, keep every source record and all identifiers. For display prefer, in order: a published record over an unknown one over a preprint; then OpenAlex, Semantic Scholar, arXiv. The longest abstract and the highest citation count are used; the DOI shown is a publisher DOI when one exists; `status` is `published` if any record is published, else `preprint`, else `unknown`. Unit tests cover the "preprint title differs slightly from the published version" case, the guards above and real-data regressions.

## 6. Output

Under `out/<run-name>/`:

- **`report.md`** (with frontmatter, directly usable in Obsidian):
  1. Run summary: parameters, number of queries, per-source status and request counts, new papers per round (saturation), total candidates, number passed.
  2. Tables of papers grouped by topic tag: score, title, authors, year, venue, **3-sentence summary (abstract-based)**, relevance reason, **links** (DOI / publisher page, arXiv, open-access PDF, OpenAlex / Semantic Scholar page), status (preprint / published).
  3. A collapsible "possibly relevant" section.
  4. Appendix: short table of screened-out papers (title, score, one-line reason); all queries with their effective hit counts; source logs.
- **`papers.csv`**, optional **`refs.bib`**, and a complete **`run.json`**.
- `litsearch report RUN_ID` regenerates the report from cache and **does not call the model again**.

Hard rule: **every link, DOI, author and year must come from fields returned by a data source; never generated or completed by the model.**

## 7. Later phases (not in version 1, but keep interfaces compatible)

- **Phase 2**: L2 skimming and PDFs. PDF acquisition order: download directly when an arXiv ID exists; otherwise use the open-access link from OpenAlex/S2; if neither exists mark `need_manual` and let the user upload. Rate-limit downloads and validate the file header and size. Close reading uses Claude's PDF support (verified 2026-10): `document` blocks accept URL / base64 / Files API; each page is processed as both text and image; request body ≤ 32 MB, ≤ 100 pages (up to 600 with a 1M-token context window); about 1,500–3,000 text tokens per page, images counted separately. Enabling `citations` yields page numbers and quoted passages, but **using it together with structured JSON output returns 400**, so work in two steps: first read freely with citations, then organize into fixed fields without citations. Scanned files without a text layer cannot be cited. Note-cache keys include the PDF's sha256.
- **Phase 3**: scheduled monitoring (incremental retrieval per topic + digest), cross-paper Q&A (read notes first, then the originals), review generation, publication-status re-check, Google Scholar alert e-mail approach, Obsidian / web UI.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Source rate limits or failures | Serial per source + backoff + caching; free API keys where obtainable; every step persisted for `--resume`; one failing source never fails the run, it is written to `source_runs` and shown in the report |
| OpenAlex daily quota exhausted without a key | Credit estimate in `--dry-run`; read `x-ratelimit-remaining`; cache; the run continues with the other sources and reports the shortage |
| Semantic Scholar mostly returning 429 | Treat as best-effort; snowballing relies on OpenAlex |
| Google Scholar blocked | Supplementary only, low frequency, random delays, optional proxy; failure does not block; fallback is the alert e-mail route (Phase 3) |
| Incomplete recall | Many queries + several sources + paging + snowballing + seed papers; evaluate with `known.txt`; show the saturation curve in the report |
| Misjudgment from abstracts only | Lenient threshold + "possibly relevant" bucket + appendix of screened-out papers with reasons; threshold changes only re-rank, they do not recompute |
| Missing abstracts | Fall back to another source's abstract; otherwise score on title only and mark "no abstract" |
| Model fabrication | Links and metadata come only from the sources; summaries are limited to the abstract and labelled as such; the model never generates citations |
| Cost runaway | `--dry-run`, confirmation step, `--max-scored`, `--max-llm-calls`, result caching |
| Network egress | Support `HTTPS_PROXY`; logs distinguish "blocked" from "no results" |

## 9. Command line and configuration

```
litsearch init                         # generate a project.md template and .env.example
litsearch run --query "..." [--query "..."] [--queries-file FILE]     # Phase 1d: queries given by hand
litsearch run --project project.md [--profile quick|standard|deep] [--rounds N] [--per-query N]
              [--sources openalex,s2,arxiv] [--openalex-mode title-abstract|fulltext|both] [--gs]
              [--saturation 3] [--max-scored N] [--max-llm-calls N] [--max-minutes N] [--top N]
              [--since YYYY-MM-DD] [--name NAME] [--db FILE] [--out-dir DIR]
              [--dry-run] [--yes] [--resume RUN_ID]
litsearch status [RUN_ID]              # what a run did: sources, queries, papers found (default: latest run)
litsearch report RUN_ID                # regenerate the report from cache, no model calls
litsearch queries RUN_ID               # export / view queries and their effective hit counts
```

`.env.example` lists every variable the program reads (a test enforces this):

```
CONTACT_EMAIL=                    # used in the User-Agent and the OpenAlex mailto parameter (identifier only)
OPENALEX_API_KEY=                 # recommended; raises the daily quota tenfold
S2_API_KEY=                       # optional
ANTHROPIC_API_KEY=                # the key of the API or of the relay in use
ANTHROPIC_BASE_URL=               # optional; base URL of an Anthropic-compatible relay (empty = official API)
L1_MODEL=                         # cheap model for L1 scoring; names depend on the API/relay, verify against its model list
QUERY_MODEL=                      # stronger model for query generation; same note
DATA_DIR=                         # default ./data
HTTPS_PROXY=
GS_MIN_DELAY=                     # default 10
GS_MAX_DELAY=                     # default 30
```

The model client (Phase 1e) is built from `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL`; model IDs are never hard-coded, because an API relay may expose different model names.

## 10. Tech stack and layout

Python 3.12; Typer (CLI); httpx (async); SQLite (standard `sqlite3` with a thin wrapper is enough); Anthropic SDK; pytest; ruff. Version 1 needs no web framework or scheduler, and no Docker (added later).

```
lit-search/
  CLAUDE.md  SPEC.md  pyproject.toml  .env.example
  litsearch/
    cli.py  config.py  db.py
    collectors/   base.py  openalex.py  semantic_scholar.py  arxiv.py  gscholar.py
    pipeline/     queries.py  retrieve.py  dedup.py  score.py  snowball.py  report.py
    llm/          client.py  prompts/
  tests/  spikes/  docs/  data/(gitignored)  out/(gitignored)
```

## 11. Phases and acceptance

**Phase 0 — Measurement (no application code): done 2026-10-16.** Scripts are in `spikes/`, conclusions in `docs/spike-findings.md`:
1. OpenAlex: search and paging, references/citations endpoints, actual abstract format, whether an API key is needed. Done.
2. Semantic Scholar: search, references, citations; behaviour under unauthenticated limits. Search, paging and limits done; references/citations still unverified (429).
3. arXiv API: request rules and returned fields. Done.
4. `scholarly` on the target machine with real queries: limits, fields, date reliability. Done (3 queries × 20 results, no block).
5. 20 results per source per query: share with abstracts, DOIs, open-access links. Done (sample reduced from 100 to 20).
6. Verify currently available model IDs. **Not done; do before Phase 1.**

**Phase 1 — Deep retrieval and funnel report (version 1)**: implement §3–§6 and §9. Acceptance:
1. Run the full flow on a real project description (including ≥ 2 snowball rounds) and obtain `report.md`, `papers.csv`, `run.json`.
2. **Recall check**: provide `known.txt` (5–10 papers the user knows to be relevant); the report shows how many were found and in which round each appeared.
3. **Spot check**: pick 20 papers at random; links open, metadata matches the source, summaries agree with abstracts (manual check).
4. Re-running with the same input hits the cache with no new model calls; `litsearch report` makes no model calls.
5. Make a source fail on purpose (for example a simulated 429 or a GS ban): the run still completes and the report shows that source as failed.
6. `--dry-run` does not call the model and prints estimates (including OpenAlex credits); after an interruption `--resume` continues.
7. Saturation statistics and per-round new counts are visible in the report.
8. Unit tests cover: de-duplication (including preprint vs. published title differences and ID/DOI normalization), the saturation stop rule, cache keys, report generation.

**Phase 2 / 3**: see §7; update this file before starting.

## 12. Fact-check status

- Verified (Anthropic docs, 2026-10): the PDF limits and citation constraints in §7.
- Verified in Phase 0 (2026-10-16): OpenAlex quota model, paging and abstract format; arXiv API rules and fields; Semantic Scholar search, paging limits and unauthenticated 429 behaviour; `scholarly` basic availability and output fields. See `docs/spike-findings.md`.
- Still to verify: Semantic Scholar references/citations endpoints, `limit` maximum and API-key path; OpenAlex API-key path; arXiv date filtering and paging depth; model IDs (before Phase 1).
