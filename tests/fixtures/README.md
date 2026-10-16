# Test fixtures

Small slices of real API responses recorded on 2026-10-16 during the Phase 0 spikes
(public bibliographic data), trimmed to the fields the collectors use. They contain no
contact e-mail, API keys or request URLs.

Deliberate deviations from the real responses:

- `s2_search.json`: the abstract of the second paper was set to `null` to cover the
  "no abstract" case (Semantic Scholar does return null abstracts).
- `s2_references.json`, `s2_citations.json`: shaped after the Semantic Scholar
  documentation; these endpoints could not be verified live in Phase 0 (HTTP 429).
- `arxiv_error.xml`: shaped after the arXiv API manual; not observed live.
- `openalex_work_refs.json`: built from the other fixtures so the IDs line up.
