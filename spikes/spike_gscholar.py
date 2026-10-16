# /// script
# requires-python = ">=3.12"
# dependencies = ["scholarly", "bibtexparser<2"]
# ///
"""Phase 0 spike: Google Scholar via `scholarly`. Low-frequency, strictly serial, stop on first block.

    uv run --python 3.12 spikes/spike_gscholar.py

Not part of run_all.py on purpose: never run concurrently with other work, never retried.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

QUERIES = {
    "q1": "developer trust acceptance of AI-generated code changes",
    "q2": "coding agent pull request review acceptance",
    "q3": "LLM explaining code changes to support code review",
}
N_PER_QUERY = 20
PAGE = 10  # scholarly fetches 10 results per page request


def load_env() -> None:
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    load_env()
    from scholarly import scholarly  # imported late so --help style errors are clear

    lo = float(os.environ.get("GS_MIN_DELAY", "10"))
    hi = float(os.environ.get("GS_MAX_DELAY", "30"))
    out: dict = {"delay_range_s": [lo, hi], "queries": {}, "blocked": None}
    samples = Path(__file__).parent / "samples"
    samples.mkdir(exist_ok=True)
    t0 = time.monotonic()

    def pause(why: str) -> None:
        d = random.uniform(lo, hi)
        print(f"  sleeping {d:.0f}s ({why})", flush=True)
        time.sleep(d)

    for qi, (qid, q) in enumerate(QUERIES.items()):
        if out["blocked"]:
            out["queries"][qid] = {"skipped": "blocked earlier"}
            continue
        if qi > 0:
            pause("between queries")
        print(f"{qid}: {q}", flush=True)
        results, times = [], []
        err = None
        try:
            it = scholarly.search_pubs(q)
            for i in range(N_PER_QUERY):
                if i > 0 and i % PAGE == 0:
                    pause("before next page")
                t = time.monotonic()
                pub = next(it)
                times.append(round(time.monotonic() - t, 2))
                results.append(dict(pub))
        except StopIteration:
            pass
        except Exception as e:  # noqa: BLE001 - scholarly raises many types on captcha/ban; record and stop GS
            err = f"{type(e).__name__}: {str(e)[:200]}"
            out["blocked"] = err
        (samples / f"gscholar_search_{qid}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1, default=str)
        )
        bibs = [r.get("bib", {}) for r in results]
        out["queries"][qid] = {
            "n": len(results),
            "error": err,
            "seconds_per_next": times,
            "bib_keys": sorted(bibs[0].keys()) if bibs else None,
            "top_level_keys": sorted(results[0].keys()) if results else None,
            "abstract": sum(1 for b in bibs if b.get("abstract")),
            "pub_year": sum(1 for b in bibs if b.get("pub_year")),
            "venue": sum(1 for b in bibs if b.get("venue")),
            "pub_url": sum(1 for r in results if r.get("pub_url")),
            "eprint_url": sum(1 for r in results if r.get("eprint_url")),
            "num_citations": sum(
                1 for r in results if r.get("num_citations") is not None
            ),
            "years": [b.get("pub_year") for b in bibs],
            "abstract_example": next(
                (b["abstract"][:200] for b in bibs if b.get("abstract")), None
            ),
        }
        print(f"  got {len(results)}" + (f", ERROR {err}" if err else ""), flush=True)

    out["wall_s"] = round(time.monotonic() - t0, 1)
    (samples / "gscholar_summary.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1)
    )
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
