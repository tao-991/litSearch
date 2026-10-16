# Phase 0 Findings (spikes)

Measured on 2026-10-16. Scripts live in `spikes/`; raw responses are written to `spikes/samples/` (gitignored). A Chinese snapshot of an earlier draft of this file is archived in `docs/zh-CN/spike-findings.md`; this English file is authoritative.

How to run (needs [uv](https://docs.astral.sh/uv/); no virtualenv setup):

```
uv run --python 3.12 spikes/run_all.py [openalex] [s2] [arxiv]   # sources run concurrently, serial within each source
uv run --python 3.12 spikes/spike_gscholar.py                    # Google Scholar, separate and low-frequency on purpose
uv run --python 3.12 spikes/spike_recall_probe.py <arxiv-id>     # is a known paper indexed, and which queries find it?
uv run --python 3.12 --with httpx spikes/make_readable.py        # raw samples -> spikes/samples/readable.md
```

Contact e-mail and API keys are read from `.env` (see `.env.example`) and are never written to code, samples or this file.

Test setup: three shared queries, 20 results per source per query. Sources run concurrently via `asyncio.gather`, but each source is serial (Google Scholar runs on its own, low frequency). Retries: 429 / 5xx / network errors with exponential backoff (2/4/8 s, `Retry-After` first), at most 3 retries; the circuit breaker opens after 3 consecutive give-ups.

- q1 `developer trust acceptance of AI-generated code changes`
- q2 `coding agent pull request review acceptance`
- q3 `LLM explaining code changes to support code review`

arXiv cannot take long natural-language sentences, so the three queries were converted by hand to boolean syntax, for example `all:"AI-generated code" AND (all:trust OR all:acceptance)`.

> The sample is only 20 results per source per query (about 60 papers per source), so percentages are rough indications.

## 0. Overview: verdict on each assumption

| # | SPEC assumption | Verdict | Notes |
|---|---|---|---|
| 1 | OpenAlex API key is optional | **Adjust** | Without a key every egress IP shares $0.10/day (about 100 searches); `mailto` does not raise it; `cursor` paging gives 401. Treat a key as effectively needed |
| 2 | OpenAlex abstracts need reconstruction | Holds | `abstract_inverted_index` → order words by position |
| 3 | OpenAlex offers references / citations | Holds | `referenced_works` + `filter=openalex:` batch fetch; `filter=cites:` for citing works |
| 4 | Semantic Scholar works unauthenticated but may 429 | **Adjust** | About 85% of requests returned 429; treat as best-effort |
| 5 | S2 references/citations usable | **Unverified** | Blocked by 429 in both runs |
| 6 | arXiv needs ≥ 3 s between requests | Holds | 12 requests at 3 s intervals, all 200; faster intervals deliberately not tested |
| 7 | arXiv has no citation data | Holds | Not offered by the API |
| 8 | One query can be reused across sources | **Adjust** | OpenAlex default search is full-text; arXiv needs boolean syntax; S2 bulk is boolean |
| 9 | Concurrency 1 per source is enough | Keep watching | Fast for OpenAlex and arXiv; S2's bottleneck is its shared quota, not concurrency |
| 10 | `scholarly` works on the target machine | Holds (with limits) | 3 queries × 20 results, no block; but snippets only, no DOI, unusable `eprint_url` (§4) |
| 11 | Model IDs | **Not done** | Out of scope here; verify before Phase 1 |
| 12 | Three hand-written queries give good recall | **Does not hold** | A known relevant paper, indexed in all three sources, was missed; see §6 |

## 1. OpenAlex

**Endpoint and authentication**
- Search: `GET https://api.openalex.org/works?search=...&per-page=20`; `mailto=` may be added.
- Billing is now **credit-based**, visible in `x-ratelimit-*` response headers and `meta.cost_usd`:
  - No key: 1000 credits/day ($0.10), shared per network egress IP. `mailto` does **not** raise it (the `meta.notice` says so).
  - Free key: $1/day (10×). Sign-up page `openalex.org/settings/api`; the response also mentions `POST /api-keys` with an e-mail address (**not used** here).
  - Observed cost per request: search (`search=`) 10 credits; list with `filter=` only 1 credit; DOI single lookup 0; error responses (400) 0. Both `search=` and `filter=title_and_abstract.search:` cost 10. A request with `per-page=200` also costs 10 credits (measured in §6d).
  - Quota reset: `x-ratelimit-reset` was about 72,500 s (roughly 20 h; trust the header).
  - Estimate: about 100 searches/day without a key, about 1000 with one. A Phase 1 run with dozens of queries plus paging will use a lot of the keyless quota.
- 14 serial requests (0.2 s interval) took 12 s; typical latency 0.3–2 s; no 429.

**Default `search` is full-text.** `meta.x_query` shows it is translated to `fulltext.search`; q1 matched 22,751 works. The first 8 titles were highly relevant (developer trust and acceptance of AI code) but the long tail is noisy. `filter=title_and_abstract.search:<whole sentence>` returned only 9 works with unrelated ones mixed in (all terms must match). Conclusion: query generation should produce short precise phrases, and a second recall path using `title_and_abstract.search` is worth evaluating.

**Paging**
- `page=N` works; pages 2 and 3 do not overlap page 1. Hard cap 10,000 records (`page=501, per-page=20` → 400 `Maximum results size of 10,000 records is exceeded`).
- `per-page` accepts 1–200 (201 → 400).
- `cursor=*` works for the first page and returns `meta.next_cursor`; **using that cursor for page two → 401 `API key required for cursor paging`**. Without a key use `page`.
- Filters: `filter=from_publication_date:2024-01-01,type:article|preprint` works; `select=` trims fields.

**Fields (useful part of a work)**: `id`, `doi` (as `https://doi.org/...`), `title`/`display_name`, `publication_year`, `publication_date`, `type` (article, preprint, conference-paper, ...), `authorships[].author.display_name`, `primary_location` (with `landing_page_url`, `pdf_url`, `source.display_name`), `locations`, `best_oa_location.pdf_url`, `open_access.oa_url`, `cited_by_count`, `referenced_works`, `ids`, `language`.
- Preprint DOIs look like `10.48550/arxiv.<id>`, so the arXiv ID can be derived.
- DOI lookup `works/doi:10.48550/arxiv.1706.03762` resolved to W2626778328, but the returned `doi` field was a different DOI: **seed resolution by DOI works, but the returned DOI may differ from the input, so de-duplication must match on several normalized keys.**
- Journal/conference level (CCF, CORE, JCR quartile) is **not provided**; only the venue name is (for example a work's `locations[].source.display_name`).

**References and citations**
- References: `referenced_works` (URL list); details in batch via `filter=openalex:W1|W2|...` (16 fetched at once, 1 credit).
- Citing works: `filter=cites:W...`; for "Attention Is All You Need" it returned `meta.count=24,511` and 20 items per page. A brand-new paper with 0 citations correctly returns an empty list.

**Coverage (3 queries × 20 = 60)**: abstract 58 (97%), DOI 55 (92%), `best_oa_location.pdf_url` 43 (72%), `open_access.oa_url` 59 (98%).

## 2. Semantic Scholar (Graph API, unauthenticated)

**Endpoints**
- Search: `GET https://api.semanticscholar.org/graph/v1/paper/search?query=...&limit=20&offset=0&fields=...`.
- Fields requested: `title,abstract,authors,year,venue,publicationDate,externalIds,openAccessPdf,citationCount,url,isOpenAccess`; all names were valid.
- Bulk search: `/paper/search/bulk` returns `data`, `token`, `total`; q1 as a whole sentence matched only 1 paper, so bulk syntax is stricter (boolean) and not suitable for natural-language queries.
- Lookup by arXiv ID `/paper/ARXIV:<id>` worked once in the recall probe (§6), returning `externalIds` (`DOI`, `ArXiv`, `DBLP`, `CorpusId`) and `publicationVenue` (name, type, aliases).

**Rate limiting (the most important result here)**
- Across the two runs, 47 requests (serial, 1 s spacing, 2/4/8 s backoff on 429): **40 were 429 and only 7 succeeded** (about 15%).
- Run 1: 1 of 17 succeeded and the breaker opened. Run 2: 5 of 29 succeeded. A single probe a few minutes later succeeded.
- 429s are independent of our request rate (1 req/s, serial); they come from the **shared unauthenticated quota being used up by others**, intermittently. There was no `Retry-After` or `x-ratelimit-*` header to use.
- Burst probe (20 back-to-back requests, no retry): the first two were already 429 and the breaker stopped it.
- An API key was not available to the tester, so the key path is **unverified**.

**Paging**
- `offset`/`limit`: offset=20 returned the second page without overlap (q1 had 28 results in total; page two had 8 and `next` was empty).
- Limit: `offset + limit` must be < 1000 (offset=990, limit=20 → 400 `Relevance search offset + limit must be < 1000. Consider '/paper/search/bulk'`).
- Maximum `limit` (> 100): unverified (429).

**Fields**: `paperId`, `title`, `abstract`, `authors`, `year`, `venue`, `publicationDate`, `externalIds` (`DOI`, `ArXiv`), `openAccessPdf.url`, `isOpenAccess`, `citationCount`, `url`.

**Coverage (one successful response each for q1/q2/q3, 60 papers from two different runs)**: abstract 57 (95%), DOI 56 (93%), arXiv ID 37 (62%), `openAccessPdf.url` 32 (53%).

**References / citations**: `/paper/{id}/references` and `/paper/{id}/citations` are **unverified** (blocked by 429 both times). In Phase 1, snowballing is OpenAlex-first with S2 as a supplement.

## 3. arXiv API

- Endpoint: `GET https://export.arxiv.org/api/query`, returns Atom XML. Parameters: `search_query`, `start`, `max_results`, `sortBy` (relevance / submittedDate), `sortOrder`, `id_list`.
- Rate: the official rule is at least 3 s between requests. 12 requests (two runs) at 3 s spacing, serial, all returned 200, no 429. Faster spacing was not tested.
- Query syntax: `all:`, `AND/OR`, parentheses and quoted phrases work. Long natural-language sentences do not; convert them.
- Paging: `start=20&max_results=20` returned page two (q1 had 22 results; page two had 2; no overlap). Maximum depth not tested.
- Response metadata: `opensearch:totalResults`, `startIndex`, `itemsPerPage`.
- Fields: `id` (like `http://arxiv.org/abs/2601.21379v1`, **with version and `http`**, so it must be normalized), `title`, `summary` (abstract), `author/name`, `published`, `updated`, `arxiv:doi`, `arxiv:journal_ref`, `arxiv:comment`, `arxiv:primary_category`, `link[title=pdf]`.
- Lookup by ID: `id_list=1706.03762` works (returned v7).
- Date filter: `submittedDate:[202401010000 TO 202612312359]` was accepted, but `totalResults` equalled the unfiltered 108 and all dates were in 2026, so **it cannot be confirmed that the filter works**; verify separately in Phase 1.
- `published` oddity: for `2506.11009` the `published` field was `2025-04-25` although the ID implies June 2025. Unexplained; do not treat arXiv dates as fully reliable.
- Coverage (3 queries, 57 papers): abstract 57 (100%), PDF link 57 (100%), author-provided `doi` only 4 (7%), `journal_ref` only 3 (5%). **Whether a preprint was later published cannot be decided from arXiv**; match it back through OpenAlex/S2. The `comment` field sometimes names the venue (for example "MSR 2025, Industry track").
- No references / citations.

## 4. Google Scholar (`scholarly`)

Script `spikes/spike_gscholar.py`, **run on its own**, never concurrently with other sources: the same 3 queries, 20 results each (10 per page, 6 page requests in all), a random 10–30 s pause between page requests, stop immediately on any block, no retry. 131 s in total.

**Result**: no captcha, no ban, no 429; 60 results retrieved (`blocked: null`). This is one test from one egress IP and **says nothing about long-term stability**.

**Install issue**: `scholarly` needs `bibtexparser` 1.x; the latest 2.x fails at import with `No module named 'bibtexparser.bibdatabase'`. Pin `bibtexparser<2` (already in the script header). `scholarly` also prints a harmless `SyntaxWarning` (regex escape).

**What comes back (`PUBLICATION_SEARCH_SNIPPET`, not filled)**
- `bib` has only `title`, `author`, `pub_year`, `venue`, `abstract`. **No DOI, no arXiv ID.**
- Top level: `gsrank`, `pub_url`, `eprint_url`, `num_citations`, `citedby_url`, `author_id`, `url_scholarbib`, ...
- **`abstract` is only a keyword snippet from the results page**, at most about 190 characters, with gaps (for example "accepted by developers? We examine whether ... Our analysis of"). **It cannot be used as an abstract for L1 scoring.** Getting the full abstract needs an extra request per paper (`fill`), which costs more and raises ban risk; not recommended.
- `author` is abbreviated (`A Ghammam`), not full names. `venue` is often truncated (`Proceedings of the 23rd ...`).
- `pub_year` is `"NA"` for 4 of 60.
- `pub_url`: 58 of 60 are valid http links (mostly publisher pages). **`eprint_url`: all 52 that appeared were `javascript:void(0)`; none usable**, so it cannot serve as a PDF link.

**Overlap with the other sources**: GS's top 20 matched by exact title the 60-paper samples of the other sources in 3/20 (q1), 11/20 (q2) and 4/20 (q3). This compares only the top 20 of each source and says nothing about quality, but GS clearly returns a different set, so it has value as supplementary recall.

**Speed**: `next()` takes about 0 s inside a page; a page change takes about 3.6–5.3 s (the actual request).

**Implications for Phase 1**
- Keep GS as a supplementary round as designed: low frequency, random delays, stop on block.
- Results must be matched back to OpenAlex by title to obtain DOI, full abstract and OA links. Unmatched ones are flagged `gs_only` and not abstract-scored.
- Ignore `eprint_url`.

## 5. Concurrency and timing

- All three sources at once: OpenAlex 12 s, arXiv 15 s, S2 65 s (including backoff waits); total 65 s, about the slowest source rather than the sum. Cross-source `asyncio.gather` works.
- One request at a time per source: enough for OpenAlex and arXiv; S2's bottleneck is the shared quota and more concurrency would only hurt.
- Concurrency inside one source (for example 5 parallel OpenAlex requests) was **not tested** (kept serial by agreement).
- Google Scholar does not take part in concurrency, by design.

## 6. Recall probe: why was a known relevant paper not found?

The tester supplied a paper they consider relevant: *Human-In-The-Loop Software Development Agents: Challenges and Future Directions* (arXiv 2506.11009, MSR 2025 Industry Track). Script: `spikes/spike_recall_probe.py`.

**Indexed everywhere, with complete data**
- arXiv: present; the `comment` field says "MSR 2025, Industry track"; no `doi`/`journal_ref` fields.
- OpenAlex: present as a conference paper with an abstract, DOI `10.1109/msr66628.2025.00112`, venue "IEEE/ACM International Conference on Mining Software Repositories (MSR)", plus two arXiv locations.
- Semantic Scholar: present (the single lookup succeeded), `publicationVenue` is the MSR conference, `externalIds` includes DOI, arXiv ID, DBLP.

**But not found by our queries**
- It appeared in none of the collected samples.
- Within the top 50 results it was absent for q1/q2/q3 in OpenAlex and in arXiv, and also for three alternative broad phrasings (`human-AI collaboration coding agents challenges`, `developers review code generated by AI agents`, `software engineering agents human oversight`).
- It ranked **2nd** in both OpenAlex and arXiv for `human-in-the-loop software development agents`, the paper's own wording.

**Diagnosis**: the sources have the paper; recall failed because the queries leaned on "trust / acceptance / pull request review" while the paper uses "human-in-the-loop" and "software development agents". Rankings are keyword-based and know nothing about the research topic. Three hand-written queries are too narrow.

**Consequences**
- Multi-angle query generation, citation snowballing and the `known.txt` recall check (SPEC §3.2, §3.6, §11) are required, not optional.
- Venue level cannot come from the sources (only venue names); a CCF/CORE/JCR mapping would be a separate feature with a user-maintained table.

## 6b. Collector smoke test (Phase 1b, 2026-10-16)

After the collectors were written (`litsearch/collectors/`), `spikes/smoke_collectors.py` ran them once against the real services with the query `human-in-the-loop software development agents`, 5 results each.

- **OpenAlex**: works. 12 credits used (one search = 10, references and citations lookups = 1 each; 805 of 1000 left afterwards). The known paper (arXiv 2506.11009) ranked 2nd; its arXiv ID was filled in correctly from the arXiv location, and the venue came from the conference rather than from arXiv. References (5) and citations (5) of the first result were fetched.
- **arXiv**: works. One request. With the "all keywords must match" conversion the known paper ranked 1st (33 matches in total).
- **Semantic Scholar**: HTTP 429 again after the 3 retries; reported as `rate_limited` without crashing the other sources. The references and citations endpoints therefore remain **unverified live**.

## 6c. De-duplication on real data (Phase 1c)

Running the de-duplicator over the saved Phase 0 results (157 records: 60 OpenAlex, 40 Semantic Scholar, 57 arXiv) gave 129 distinct papers: 115 seen in one source, 11 in two, 3 in all three. Two things in the real data shaped the rules:

- **OpenAlex can return one paper's title with another paper's identifiers.** One work had the title of a 2026 arXiv paper but the arXiv ID, year and authors of a 2022 paper. Title-only matching must therefore also require compatible years, authors and identifiers, which the de-duplicator does.
- **Repository DOIs are not publisher DOIs.** The same MSR paper appeared with its ACM DOI and with a Zenodo DOI; a Security-in-the-age-of-AI-teammates paper appeared with arXiv, SSRN, Zenodo and journal DOIs. Treating repository DOIs as "a different publisher DOI" kept such copies apart, so they are now ignored by the "different DOIs" safeguard.

## 6d. Why do sources overlap so little for the same phrase? (Phase 1c, 2026-10-16)

Query `human-in-the-loop software development agents`, 20 results per source: of 40 records only 2 were the same paper (OpenAlex and arXiv; Semantic Scholar was rate-limited). Checks:

- OpenAlex **does contain** all 18 arXiv-only papers (looked up by arXiv DOI; single lookups cost 0 credits). The sources do not disagree about what exists; they disagree about what ranks first.
- The two sources apply **different matching rules** to the same words. arXiv (`all:` on every keyword) matched 33 papers in total. OpenAlex's default `search` is a full-text relevance search with 284,120 matches. Even the first 200 OpenAlex results contained only 6 of arXiv's 33 papers.
- Restricting OpenAlex to titles and abstracts (`filter=title_and_abstract.search:`) gave 343 matches. Fetching them all (2 requests) and comparing the complete sets: 17 of arXiv's 33 papers are in OpenAlex's set; OpenAlex's set contains 45 papers with an arXiv copy (28 not found by arXiv's query). Why the remaining 16 arXiv papers are missing from OpenAlex's title/abstract set was not investigated (word forms, missing abstracts or arXiv matching other fields are possible, none verified).
- **A request with `per-page=200` costs 10 credits, the same as one with `per-page=20`.** Credits are charged per request, not per paper, so pages should be as large as useful (up to 200).

Consequences: overlap between sources is expected to be small and is not the goal; the union is. Query generation, paging and snowballing (SPEC §3) matter more than de-duplication volume. For OpenAlex, a title-and-abstract search mode is worth offering next to the full-text mode: it is far less noisy and its complete result set can be fetched cheaply.

## 6e. First end-to-end run of `litsearch run` (Phase 1d, 2026-10-16)

Three queries, quick profile (15 papers per query and source), all three sources: 9 requests, about 40 s in total. OpenAlex (title-and-abstract mode) returned 45 results for 30 credits and arXiv 42; Semantic Scholar answered the first query (15 results) and returned HTTP 429 for the other two, even after three retries each, so its status was `rate_limited` while the run completed. 102 records became 73 distinct papers. `--resume` then listed exactly the 2 failed Semantic Scholar fetches as the remaining work and nothing else. The credit estimate (30) matched the quota actually used, and the remaining-quota figure read from the free lookup matched the earlier measurements.

## 6f. Model access through an API relay (Phase 1e, 2026-10-16)

A relay that lists 146 models (19 Claude, 63 GPT, 16 Gemini, plus DeepSeek, Qwen, GLM, Kimi, ...) was tested with tiny requests.

- **One endpoint, two request formats.** Claude models answered in the Anthropic message format (`/v1/messages`); a GPT model was refused there ("the interface does not support this model") but answered in the OpenAI chat format (`/v1/chat/completions`). Gemini and DeepSeek models also answered in the OpenAI format. The same key and base URL worked for both, which is why the client chooses the format per model name.
- The relay's base URL ended in `/v1`; the client strips it and adds the path itself, so both spellings work.
- All of `claude-haiku-4-5-20251001`, `claude-sonnet-5-5`, `gpt-4.1-nano`, `gpt-4o-mini`, `gemini-3.5-flash-lite`, `deepseek-v4-flash` and the configured `gpt-6.1-sol` answered "OK" in 1–3 s. **`gpt-5-nano` returned an empty reply for `max_tokens=64`: it is a reasoning model and spent all 64 tokens on hidden reasoning.** Scoring prompts must use a non-reasoning model or a generous token budget.
- Prices of the relay's models were not measured; they have to be read from the relay's price list.

## 6g. Letting the model decide how many queries (Phase 1e, 2026-10-16)

`litsearch brief` and `litsearch queries` were run against the relay (`gpt-6.1-sol`) for the project "how can a coding agent help engineers decide faster whether to accept generated changes". The brief it wrote (in Chinese, as configured) restated the question, listed concepts and angles, said that no related work had been given instead of inventing some, and put eight open questions under "to clarify".

- **A number in the prompt becomes the target.** With the instruction "never write more than N queries" the model wrote exactly 15 queries for N = 15 and 54 for N = 60, and even a one-question topic (Python libraries for the Gini coefficient) got 10 queries, some with names that looked invented.
- **Fix:** the prompt no longer contains any number; it asks the model to judge the breadth of the topic, not to pad, not to invent names, and to list the queries from most to least important. The ceiling is applied afterwards (the least important queries are cut). Result with the same model: 26 queries for the broad topic (15 kept under the quick ceiling, all 26 with a ceiling of 60) and 2 queries for the one-question topic.
- A cost of about 700 input and 700–1250 output tokens per query-writing call, and about 220 in / 400 out for a brief.
- **Too many even then.** 26 queries was judged far too broad for a literature review (the prompt had asked to cover adjacent fields and human-factors angles). The prompt was changed to ask for a *focused* review: precision first, another query only if it finds papers the earlier ones would miss, no branching into adjacent fields. Result with the same model and no ceiling shown to it: 5 queries for the broad topic and 1 for the one-question topic. The profile ceilings were lowered to 5 / 10 / 20.

## 6h. PDF input (Phase 1e, 2026-10-16)

`litsearch brief --pdf` was tried with the MSR 2025 paper *Human-In-The-Loop Software Development Agents: Challenges and Future Directions* (arXiv 2506.11009, 3 pages, 47 KB).

- The text of all 3 pages was extracted locally; the 6,000-character budget cut it. The model wrote a brief that stated that the user had given no research description yet, summarised the paper's topic (testing cost, instability of LLM similarity scores) and listed the paper itself under related work by its printed title; open questions asked what the user wants to do with it. The brief cost 1,640 input and 417 output tokens.
- From that brief the model wrote 6 focused queries (for example "LLM code evaluation stability", "SWE-bench environment setup").
- `pypdf` logged one "fontTools is required to fully parse the encoding of a CFF Type1 font" warning per font for this ordinary LaTeX paper although the text came out fine; the warnings are silenced.

## 7. Suggested SPEC changes

These were applied in SPEC v0.3:

1. OpenAlex: credit-based quota, key recommended, `mailto` is an identifier only, paging via `page`.
2. `--dry-run` reports estimated OpenAlex credits and compares them with `x-ratelimit-remaining`.
3. Semantic Scholar becomes a best-effort source; snowballing depends on OpenAlex.
4. Query generation: short phrases, per-source syntax adapters, recall depends on wording.
5. Google Scholar: snippets only, match back to OpenAlex, ignore `eprint_url`, pin `bibtexparser<2`.
6. De-duplication normalization details (arXiv ID, DOI, derivation from `10.48550/arxiv.*`).
7. Fact-check status updated.

## 8. Not done / unverified

- Model ID check (before Phase 1).
- Semantic Scholar: `references`/`citations` endpoints, `limit` maximum, token paging, API-key path (the tester could not apply for a key).
- OpenAlex: API-key path, full `cursor` paging, concurrency inside one source (the tester could not obtain a key).
- arXiv: behaviour below 3 s (deliberately not tested), maximum paging depth, whether the date filter works.
- Google Scholar: one test, 3 queries, one egress IP; long-term ban risk, proxies and `fill` for full abstracts (deliberately not tested).
- The Phase 0 runs used roughly 110–150 credits of the tester's daily OpenAlex quota (1000).
