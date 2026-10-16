# Lit Search

Deep, multi-source, auditable literature search for a research project.

Asking a chat assistant to search the literature tends to end in rate limits, incomplete results, two or three rounds of searching, and no way to tell how much was covered. Lit Search is meant to fix that: give it a project description (plus optional keywords and seed papers) and it runs many queries across several scholarly sources, screens the results through a "reading funnel", and writes a ranked report. The report also records which queries were used, which sources succeeded or failed, and how many new relevant papers each round added, so coverage can be checked rather than guessed.

> **Status: early. Phase 0 (API measurement) is done; Phase 1 (the application) is in progress.** There is no end-to-end search command yet: `litsearch` currently only prints its version. What exists is the specification, the measured behaviour of the data sources, the throwaway scripts behind those measurements, the foundations (configuration, SQLite schema), one collector per data source (OpenAlex, Semantic Scholar, arXiv) that fetches and normalizes papers, and a de-duplicator that merges records of the same paper. These pieces are not wired into a pipeline or the database yet.

Phase 1 progress:

- [x] **1a** Project skeleton, configuration, database schema, tests and lint
- [x] **1b** Collectors for OpenAlex, Semantic Scholar and arXiv (offline-tested; smoke-tested against the real services)
- [x] **1c** De-duplication (merges the same paper found by different sources or queries)
- [ ] **1d** Retrieval pipeline, `--dry-run`, `--resume`
- [ ] **1e** Model client, query generation, L1 scoring
- [ ] **1f** Snowballing, report, CSV, full command line, optional Google Scholar round

## Planned behaviour (version 1)

1. **Generate queries.** A model turns the project description into many short, diverse queries (synonyms, method names, adjacent terms). You can edit them and re-run.
2. **Retrieve from several sources.** OpenAlex, Semantic Scholar and arXiv, paged beyond the first page; Google Scholar as an optional, low-frequency supplement.
3. **De-duplicate** by DOI, arXiv ID and fuzzy title match (preprint and published versions are merged).
4. **Screen (L1).** A cheap model reads title and abstract in batches and returns a score, a reason, a 3-sentence summary and tags. Borderline papers go to a "possibly relevant" bucket instead of being dropped.
5. **Snowball.** Take the best papers, fetch their references and citing papers, and repeat for a few rounds until new relevant results dry up.
6. **Report.** Markdown report, CSV, and a full run record. Links, DOIs, authors and years come only from the data sources; the model never writes them.

What you get is a report under `out/<run-name>/` (`report.md` is written to be dropped into Obsidian). The SQLite database under `data/` is only a disposable working cache (resuming interrupted runs, avoiding paying twice for the same model call, recording which sources failed); it is not a literature library, and you can delete it at any time.

Cost control is part of the design: a `--dry-run` estimate before any model call, a confirmation step, call and paper limits, `--resume` for interrupted runs, and caching of every model result. A failing data source never fails the run; it is recorded and shown in the report.

Later phases (not in version 1): skimming PDFs, scheduled monitoring, cross-paper Q&A, review drafts, a conversational front end. See [`SPEC.md`](SPEC.md).

## What Phase 0 found

Measured on 2026-10-16 with real requests (details and caveats in [`docs/spike-findings.md`](docs/spike-findings.md)):

- **OpenAlex** is the strongest source (abstracts for about 97% of the sampled results, usable references and citations), but it is now credit-based: without an API key every network egress IP shares about 100 searches per day, and `mailto` does not raise that. A free key is recommended.
- **Semantic Scholar** without a key was unreliable: about 85% of requests returned HTTP 429, even when sent serially at 1 request per second. It is treated as best-effort.
- **arXiv** works as documented (one request every 3 seconds), has full abstracts, but no citation data and few DOIs.
- **Google Scholar** (via `scholarly`) worked at low frequency, but only returns search snippets: no DOI, abbreviated authors, and no usable PDF link. Results must be matched back to OpenAlex.
- **Recall depends on wording.** A relevant paper that is indexed in all three sources was missed by three hand-written queries and found at rank 2 with its own terminology. This is why the design leans on query generation, snowballing and a recall check against papers you already know.

## Repository layout

```
litsearch/              the application (config, db, models, collectors; more to come)
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

# optional: a few real requests against the three data sources (about 12 OpenAlex credits)
uv run python spikes/smoke_collectors.py "your search phrase"

# optional: live demo - search the three sources and de-duplicate (about 10 OpenAlex credits)
uv run python spikes/demo_search.py "your search phrase"

# optional: see how the de-duplicator merges the saved Phase 0 results (writes spikes/samples/dedup.md)
uv run python spikes/show_dedup.py
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

Everything tunable goes through environment variables or CLI flags; nothing is hard-coded. See [`.env.example`](.env.example). `CONTACT_EMAIL` is only used as an identifier in the `User-Agent` header and the OpenAlex `mailto` parameter. Never commit `.env`.

## Scope and etiquette

Lit Search does not bypass paywalls, scrape publisher sites or simulate logins, and it does not send frequent or concurrent requests to Google Scholar.

## License

[MIT](LICENSE)
