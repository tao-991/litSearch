# Lit Search

Deep, multi-source, auditable literature search for a research project.

Asking a chat assistant to search the literature tends to end in rate limits, incomplete results, two or three rounds of searching, and no way to tell how much was covered. Lit Search is meant to fix that: give it a project description (plus optional keywords and seed papers) and it runs many queries across several scholarly sources, screens the results through a "reading funnel", and writes a ranked report. The report also records which queries were used, which sources succeeded or failed, and how many new relevant papers each round added, so coverage can be checked rather than guessed.

> **Status: early. Phase 0 (API measurement) is done; Phase 1 (the application) is in progress.** `litsearch run` already searches OpenAlex, Semantic Scholar and arXiv for the queries you give it, merges duplicates, stores everything in a local SQLite file and writes a candidate list (CSV and Markdown). What does **not** exist yet: generating queries from a description or a PDF, scoring papers with a model, citation snowballing and the final report. The candidate list is therefore not ranked by relevance to your project, only by how many of your queries found each paper.

Phase 1 progress:

- [x] **1a** Project skeleton, configuration, database schema, tests and lint
- [x] **1b** Collectors for OpenAlex, Semantic Scholar and arXiv (offline-tested; smoke-tested against the real services)
- [x] **1c** De-duplication (merges the same paper found by different sources or queries)
- [x] **1d** Retrieval pipeline: `litsearch run` / `litsearch status`, `--dry-run` estimate, confirmation, `--resume`, time limit, profiles
- [ ] **1e** Model client, query generation from free text or PDFs, L1 scoring
- [ ] **1f** Snowballing, report, full command line, optional Google Scholar round

## Quick start

Requires [uv](https://docs.astral.sh/uv/).

```bash
make sync                      # install dependencies
cp .env.example .env           # optional: add CONTACT_EMAIL and API keys (see Configuration)

# 1. see what a search would cost, without fetching anything
uv run litsearch run -q "human-in-the-loop software development agents" \
                     -q "AI-generated code review acceptance" --dry-run

# 2. run it (asks for confirmation; add --yes to skip)
uv run litsearch run -q "human-in-the-loop software development agents" \
                     -q "AI-generated code review acceptance"

# 3. look at the result
uv run litsearch status        # sources, queries, papers found
open out/run-0001/candidates.md      # or candidates.csv (all papers, with abstracts)
```

Useful options (see `litsearch run --help`): `--queries-file FILE` (one query per line, `#` for comments), `--profile quick|standard|deep` (how much to search; `quick` is the default), `--per-query N`, `--sources openalex,s2,arxiv`, `--openalex-mode title-abstract|fulltext|both`, `--since 2024-01-01`, `--max-minutes N` (stop cleanly at the limit), `--name NAME`, and `--resume RUN_ID` to continue an interrupted run or to retry the fetches that failed. A failing source (Semantic Scholar often is rate-limited without an API key) is reported but never stops the run.

Results are written to `out/<run-name>/` and to a SQLite file at `data/litsearch.db`; both are gitignored, and the database is only a working cache that you may delete.

## Planned behaviour (version 1)

1. **Generate queries.** *(planned; for now you give the queries yourself)* You describe your research interest in plain language and/or hand over one or more PDFs; a model turns that into many short, diverse queries (synonyms, method names, adjacent terms) and shows you its understanding of the topic for confirmation. You can edit the queries and re-run.
2. **Retrieve from several sources.** OpenAlex (titles and abstracts by default), Semantic Scholar and arXiv; Google Scholar as an optional, low-frequency supplement. *(done, except Google Scholar)*
3. **De-duplicate** by DOI, arXiv ID and fuzzy title match (preprint and published versions are merged). *(done)*
4. **Screen (L1).** A cheap model reads title and abstract in batches and returns a score, a reason, a 3-sentence summary and tags. Borderline papers go to a "possibly relevant" bucket instead of being dropped.
5. **Snowball.** Take the best papers, fetch their references and citing papers, and repeat for a few rounds until new relevant results dry up.
6. **Report.** Markdown report, CSV, and a full run record. Links, DOIs, authors and years come only from the data sources; the model never writes them.

What you get is a report under `out/<run-name>/` (`report.md` is written to be dropped into Obsidian). The SQLite database under `data/` is only a disposable working cache (resuming interrupted runs, avoiding paying twice for the same model call, recording which sources failed); it is not a literature library, and you can delete it at any time.

Cost control is part of the design: a `--dry-run` estimate before anything is fetched, a confirmation step, a time limit, call and paper limits, `--resume` for interrupted runs, and caching of every model result. A failing data source never fails the run; it is recorded and shown in the output. The default size is the `quick` profile (about 8 queries, 15 papers per query and source, one snowball round, the model reads at most 150 papers, 10 minutes); `standard` and `deep` search wider. You read only the short ranked list at the top of the report, not everything that was retrieved.

Later phases (not in version 1): skimming PDFs, scheduled monitoring, cross-paper Q&A, review drafts, a conversational front end. See [`SPEC.md`](SPEC.md).

## What Phase 0 found

Measured on 2026-10-16 with real requests (details and caveats in [`docs/spike-findings.md`](docs/spike-findings.md)):

- **OpenAlex** is the strongest source (abstracts for about 97% of the sampled results, usable references and citations), but it is now credit-based: without an API key every network egress IP shares about 1000 credits per day, a search request costs 10 credits whether it asks for 20 or 200 results, and `mailto` does not raise that. A free key is recommended. Its plain search is a noisy full-text search, so the default mode searches titles and abstracts only.
- **Semantic Scholar** without a key was unreliable: most requests returned HTTP 429, even when sent serially at 1 request per second (occasionally a request succeeds). It is treated as best-effort.
- **arXiv** works as documented (one request every 3 seconds), has full abstracts, but no citation data and few DOIs.
- **Google Scholar** (via `scholarly`) worked at low frequency, but only returns search snippets: no DOI, abbreviated authors, and no usable PDF link. Results must be matched back to OpenAlex.
- **Recall depends on wording.** A relevant paper that is indexed in all three sources was missed by three hand-written queries and found at rank 2 with its own terminology. This is why the design leans on query generation, snowballing and a recall check against papers you already know.

## Repository layout

```
litsearch/              the application: config, db, models, collectors/ (one per source),
                        pipeline/ (dedup, store, retrieve, runner, export), cli
tests/                  unit tests (never touch the network)
pyproject.toml          project metadata, dependencies, ruff and pytest settings
uv.lock                 exact dependency versions (generated by uv, do not edit)
Makefile                shortcuts: sync, test, lint, format
SPEC.md                 specification (authoritative)
CLAUDE.md               working rules for the AI coding assistant used on this repo
docs/spike-findings.md  Phase 0 measurements
docs/zh-CN/             archived Chinese drafts (not kept in sync)
spikes/                 throwaway scripts used for the Phase 0 measurements
.env.example            configuration variables
```

## Development

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is selected automatically.

```bash
make sync     # create .venv and install dependencies
make test     # pytest (no network access)
make lint     # ruff check + ruff format --check
make format   # apply ruff fixes and formatting
uv run litsearch --version


# optional helper scripts in spikes/ (real requests, small):
uv run python spikes/smoke_collectors.py "your search phrase"   # try the three collectors (about 12 OpenAlex credits)
uv run python spikes/demo_search.py "your search phrase"        # search and de-duplicate, nothing stored
uv run python spikes/show_dedup.py                              # de-duplicate the saved Phase 0 results (no network)
```

## Running the Phase 0 spikes

The spikes make real network requests, are deliberately gentle (serial per source, backoff on 429, 3-second spacing for arXiv, random 10–30 second pauses for Google Scholar), and are not part of any test suite. Requirements: [uv](https://docs.astral.sh/uv/). Dependencies are declared in each script's header, so no virtual environment is needed.

```bash
cp .env.example .env          # then fill in CONTACT_EMAIL (optional) and any API keys you have

uv run --python 3.12 spikes/run_all.py                   # OpenAlex + Semantic Scholar + arXiv, concurrently across sources
uv run --python 3.12 spikes/spike_gscholar.py            # Google Scholar, separate on purpose
uv run --python 3.12 spikes/spike_recall_probe.py 2506.11009   # is a known paper indexed, and which queries find it?
uv run --python 3.12 --with httpx spikes/make_readable.py      # raw responses -> spikes/samples/readable.md
```

Raw responses are written to `spikes/samples/` (gitignored). Please keep the request volume low: OpenAlex without a key has a small shared daily budget, and Google Scholar will block aggressive clients.

## Configuration

Everything tunable goes through environment variables or CLI flags; nothing is hard-coded. See [`.env.example`](.env.example): `CONTACT_EMAIL` (identifier in the `User-Agent` header and the OpenAlex `mailto` parameter, optional), `OPENALEX_API_KEY` (recommended, free), `S2_API_KEY` (optional), `DATA_DIR` (default `./data`). Never commit `.env`.

## Scope and etiquette

Lit Search does not bypass paywalls, scrape publisher sites or simulate logins, and it does not send frequent or concurrent requests to Google Scholar.

## License

[MIT](LICENSE)
