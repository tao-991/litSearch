# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Follow-up probes after the first full run (small, serial)."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
from _common import QUERIES, Source, load_env, save_sample

OA = "https://api.openalex.org"
S2 = "https://api.semanticscholar.org/graph/v1"


async def main() -> None:
    load_env()
    email = os.environ.get("CONTACT_EMAIL")
    secret = {"mailto": email} if email else {}
    out: dict = {}
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers={"User-Agent": "litsearch-spike/0.1"}
    ) as c:
        oa = Source("openalex", c, 0.3)
        q1 = QUERIES["q1"]
        # cursor401: fetch cursor1 again, then page with the returned cursor; keep the error body
        r = await oa.get(
            f"{OA}/works",
            "cursor1",
            params={"search": q1, "per-page": 5, "cursor": "*", "select": "id"},
            secret_params=secret,
        )
        nxt = r.json()["meta"]["next_cursor"] if r else None
        out["next_cursor_sample"] = (nxt or "")[:40]
        r = await oa.get(
            f"{OA}/works",
            "cursor2",
            params={"search": q1, "per-page": 5, "cursor": nxt, "select": "id"},
            secret_params=secret,
        )
        out["cursor2"] = {
            "status": r.status_code if r else None,
            "body": (r.text[:400] if r else None),
        }
        # title_and_abstract.search vs default (fulltext) search
        r = await oa.get(
            f"{OA}/works",
            "title_abstract_filter",
            params={
                "filter": f"title_and_abstract.search:{q1}",
                "per-page": 20,
                "select": "id,title,publication_year,abstract_inverted_index",
            },
            secret_params=secret,
        )
        b = r.json() if r else {}
        out["title_and_abstract_search"] = {
            "status": r.status_code if r else None,
            "count": (b.get("meta") or {}).get("count"),
            "titles": [w["title"] for w in b.get("results", [])][:8],
            "abstract": sum(
                1 for w in b.get("results", []) if w.get("abstract_inverted_index")
            ),
            "body_if_error": (
                r.text[:300] if r is not None and r.status_code != 200 else None
            ),
        }
        # default search titles for comparison
        r = await oa.get(
            f"{OA}/works",
            "default_search_titles",
            params={"search": q1, "per-page": 20, "select": "id,title"},
            secret_params=secret,
        )
        out["default_search_titles"] = [
            w["title"] for w in (r.json().get("results", []) if r else [])
        ][:8]
        # cited-by on a well-known paper
        r = await oa.get(
            f"{OA}/works",
            "cited_by_known",
            params={
                "filter": "cites:W2626778328",
                "per-page": 20,
                "select": "id,title,publication_year",
            },
            secret_params=secret,
        )
        b = r.json() if r else {}
        out["cited_by_known"] = {
            "status": r.status_code if r else None,
            "n": len(b.get("results", [])),
            "meta_count": (b.get("meta") or {}).get("count"),
        }

        # S2: single unauthenticated request again, a few minutes after the first run
        s2 = Source("s2", c, 1.0)
        headers = (
            {"x-api-key": os.environ["S2_API_KEY"]}
            if os.environ.get("S2_API_KEY")
            else None
        )
        r = await s2.get(
            f"{S2}/paper/search",
            "s2_retry_probe",
            params={"query": q1, "limit": 20, "fields": "title,year"},
            headers=headers,
            retry=False,
        )
        out["s2_retry_probe"] = {
            "status": r.status_code if r else s2.log[-1].get("status"),
            "rate_headers": s2.log[-1].get("rate_headers"),
        }
        out["logs"] = oa.log[-3:] + s2.log
    save_sample("followup.json", out)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str)[:6000])


if __name__ == "__main__":
    asyncio.run(main())
