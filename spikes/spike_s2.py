"""Phase 0 spike: Semantic Scholar Graph API (unauthenticated unless S2_API_KEY is set)."""

from __future__ import annotations

import os
from typing import Any

import httpx
from _common import N_PER_QUERY, QUERIES, Source, has_text, save_sample

BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = "title,abstract,authors,year,venue,publicationDate,externalIds,openAccessPdf,citationCount,url,isOpenAccess"


def _body(resp: httpx.Response | None) -> dict[str, Any]:
    if resp is None:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text[:300]}


async def run(client: httpx.AsyncClient) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    src = Source("s2", client, min_interval=1.0)
    headers = (
        {"x-api-key": os.environ["S2_API_KEY"]}
        if os.environ.get("S2_API_KEY")
        else None
    )
    out: dict[str, Any] = {"auth": {"api_key": headers is not None}}
    first: dict[str, list[dict[str, Any]]] = {}

    # 1. search, 20 per query
    for qid, q in QUERIES.items():
        r = await src.get(
            f"{BASE}/paper/search",
            f"search:{qid}",
            params={"query": q, "limit": N_PER_QUERY, "offset": 0, "fields": FIELDS},
            headers=headers,
        )
        body = _body(r)
        data = body.get("data", [])
        first[qid] = data
        save_sample(f"s2_search_{qid}.json", body)
        out[f"search_{qid}"] = {
            "status": r.status_code if r else None,
            "total": body.get("total"),
            "next": body.get("next"),
            "n": len(data),
            "abstract": sum(1 for p in data if has_text(p.get("abstract"))),
            "doi": sum(1 for p in data if (p.get("externalIds") or {}).get("DOI")),
            "arxiv": sum(1 for p in data if (p.get("externalIds") or {}).get("ArXiv")),
            "oa_pdf_url": sum(
                1 for p in data if (p.get("openAccessPdf") or {}).get("url")
            ),
        }
        if qid == "q1" and data:
            out["paper_field_names"] = sorted(data[0].keys())
            out["first_paper"] = {
                k: data[0].get(k)
                for k in (
                    "paperId",
                    "title",
                    "year",
                    "publicationDate",
                    "venue",
                    "externalIds",
                    "openAccessPdf",
                    "isOpenAccess",
                )
            }
        if not r:
            out[f"search_{qid}"]["body"] = "no response (failed / skipped)"

    q1 = QUERIES["q1"]
    # 2. offset paging
    for off in (20, 40):
        r = await src.get(
            f"{BASE}/paper/search",
            f"offset{off}",
            params={
                "query": q1,
                "limit": N_PER_QUERY,
                "offset": off,
                "fields": "title,year",
            },
            headers=headers,
        )
        b = _body(r)
        ids1 = {p["paperId"] for p in first["q1"]}
        ids = [p["paperId"] for p in b.get("data", [])]
        out[f"offset{off}"] = {
            "status": r.status_code if r else None,
            "n": len(ids),
            "next": b.get("next"),
            "overlap_with_page1": len(ids1 & set(ids)),
        }
    # limits: offset+limit > 1000, limit > 100
    r = await src.get(
        f"{BASE}/paper/search",
        "deep_offset990",
        params={"query": q1, "limit": 20, "offset": 990, "fields": "title"},
        headers=headers,
    )
    out["deep_offset990"] = {
        "status": r.status_code if r else None,
        "body": str(_body(r))[:300],
    }
    r = await src.get(
        f"{BASE}/paper/search",
        "limit101",
        params={"query": q1, "limit": 101, "fields": "title"},
        headers=headers,
    )
    out["limit101"] = {
        "status": r.status_code if r else None,
        "body": str(_body(r))[:300],
    }

    # 3. bulk search with token
    r = await src.get(
        f"{BASE}/paper/search/bulk",
        "bulk",
        params={"query": q1, "fields": "title,year"},
        headers=headers,
    )
    b = _body(r)
    out["bulk"] = {
        "status": r.status_code if r else None,
        "total": b.get("total"),
        "n": len(b.get("data", [])),
        "has_token": bool(b.get("token")),
        "keys": sorted(b.keys()),
    }

    # 4. seed resolution by arXiv id
    r = await src.get(
        f"{BASE}/paper/ARXIV:1706.03762",
        "lookup_arxiv",
        params={"fields": "title,year,externalIds"},
        headers=headers,
    )
    out["lookup_arxiv"] = {
        "status": r.status_code if r else None,
        "body": str(_body(r))[:300],
    }

    # 5. references / citations
    seed = next((p for p in first["q2"] if p.get("paperId")), None)
    if seed:
        pid = seed["paperId"]
        out["seed"] = {"paperId": pid, "citationCount": seed.get("citationCount")}
        for kind, key in (("references", "citedPaper"), ("citations", "citingPaper")):
            r = await src.get(
                f"{BASE}/paper/{pid}/{kind}",
                kind,
                params={
                    "fields": "title,year,abstract,externalIds",
                    "limit": N_PER_QUERY,
                },
                headers=headers,
            )
            b = _body(r)
            data = b.get("data", [])
            save_sample(f"s2_{kind}.json", b)
            out[kind] = {
                "status": r.status_code if r else None,
                "n": len(data),
                "next": b.get("next"),
                "offset": b.get("offset"),
                "item_keys": sorted(data[0].keys()) if data else None,
                "inner_key": key,
                "abstract": sum(
                    1 for d in data if has_text((d.get(key) or {}).get("abstract"))
                ),
                "null_inner": sum(
                    1 for d in data if not (d.get(key) or {}).get("paperId")
                ),
            }
    else:
        out["seed"] = None

    # 6. burst probe: 20 back-to-back requests with no delay and no retry; stop on breaker
    src.min_interval = 0.0
    statuses: list[Any] = []
    for i in range(20):
        r = await src.get(
            f"{BASE}/paper/ARXIV:1706.03762",
            f"burst{i}",
            params={"fields": "title"},
            headers=headers,
            retry=False,
        )
        statuses.append(r.status_code if r else (src.log[-1].get("status")))
        if src.breaker_open:
            break
    out["burst20"] = statuses
    return out, src.log
