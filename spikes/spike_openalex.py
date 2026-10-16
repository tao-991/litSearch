"""Phase 0 spike: OpenAlex. Search, paging, filters, abstract format, refs/citations."""

from __future__ import annotations

import os
from typing import Any

import httpx
from _common import N_PER_QUERY, QUERIES, Source, save_sample

BASE = "https://api.openalex.org"


def reconstruct_abstract(inv: dict[str, list[int]] | None) -> str | None:
    if not inv:
        return None
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _body(resp: httpx.Response | None) -> dict[str, Any]:
    if resp is None:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text[:300]}


async def run(client: httpx.AsyncClient) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    src = Source("openalex", client, min_interval=0.2)
    secret: dict[str, str] = {}
    if os.environ.get("CONTACT_EMAIL"):
        secret["mailto"] = os.environ["CONTACT_EMAIL"]
    if os.environ.get("OPENALEX_API_KEY"):
        secret["api_key"] = os.environ["OPENALEX_API_KEY"]
    out: dict[str, Any] = {
        "auth": {"mailto": "mailto" in secret, "api_key": "api_key" in secret}
    }
    first_results: dict[str, list[dict[str, Any]]] = {}

    # 1. search, 20 per query + coverage stats
    for qid, q in QUERIES.items():
        r = await src.get(
            f"{BASE}/works",
            f"search:{qid}",
            params={"search": q, "per-page": N_PER_QUERY},
            secret_params=secret,
        )
        body = _body(r)
        results = body.get("results", [])
        first_results[qid] = results
        save_sample(f"openalex_search_{qid}.json", body)
        n = len(results)
        abs_ok = sum(1 for w in results if w.get("abstract_inverted_index"))
        doi_ok = sum(1 for w in results if w.get("doi"))
        oa_pdf = sum(
            1 for w in results if (w.get("best_oa_location") or {}).get("pdf_url")
        )
        oa_any = sum(1 for w in results if (w.get("open_access") or {}).get("oa_url"))
        out[f"search_{qid}"] = {
            "status": r.status_code if r else None,
            "meta": body.get("meta"),
            "n": n,
            "abstract": abs_ok,
            "doi": doi_ok,
            "oa_pdf_url": oa_pdf,
            "oa_url": oa_any,
        }
        if qid == "q1" and results:
            out["work_field_names"] = sorted(results[0].keys())
            w = next((w for w in results if w.get("abstract_inverted_index")), None)
            if w:
                out["abstract_example"] = (
                    reconstruct_abstract(w["abstract_inverted_index"]) or ""
                )[:200]
            out["first_work_summary"] = {
                k: (w or results[0]).get(k)
                for k in (
                    "id",
                    "title",
                    "publication_year",
                    "publication_date",
                    "type",
                    "doi",
                    "language",
                )
            }
            out["ids_field"] = results[0].get("ids")
            out["primary_location"] = results[0].get("primary_location")
            out["authorships_example"] = (results[0].get("authorships") or [{}])[0]

    q1 = QUERIES["q1"]
    # 2. paging: page=2,3 ; cursor
    for p in (2, 3):
        r = await src.get(
            f"{BASE}/works",
            f"page{p}",
            params={"search": q1, "per-page": N_PER_QUERY, "page": p},
            secret_params=secret,
        )
        body = _body(r)
        ids1 = {w["id"] for w in first_results["q1"]}
        ids = [w["id"] for w in body.get("results", [])]
        out[f"page{p}"] = {
            "status": r.status_code if r else None,
            "n": len(ids),
            "overlap_with_page1": len(ids1 & set(ids)),
        }
    r = await src.get(
        f"{BASE}/works",
        "cursor1",
        params={"search": q1, "per-page": N_PER_QUERY, "cursor": "*"},
        secret_params=secret,
    )
    body = _body(r)
    nxt = (body.get("meta") or {}).get("next_cursor")
    out["cursor1"] = {
        "status": r.status_code if r else None,
        "n": len(body.get("results", [])),
        "next_cursor": bool(nxt),
    }
    if nxt:
        r = await src.get(
            f"{BASE}/works",
            "cursor2",
            params={"search": q1, "per-page": N_PER_QUERY, "cursor": nxt},
            secret_params=secret,
        )
        b2 = _body(r)
        out["cursor2"] = {
            "status": r.status_code if r else None,
            "n": len(b2.get("results", [])),
            "overlap_with_cursor1": len(
                {w["id"] for w in body.get("results", [])}
                & {w["id"] for w in b2.get("results", [])}
            ),
        }
    # deep paging limit and per-page limit
    r = await src.get(
        f"{BASE}/works",
        "deep_page501",
        params={"search": q1, "per-page": N_PER_QUERY, "page": 501},
        secret_params=secret,
    )
    out["deep_page501"] = {
        "status": r.status_code if r else None,
        "body": str(_body(r))[:300],
    }
    r = await src.get(
        f"{BASE}/works",
        "per_page_201",
        params={"search": q1, "per-page": 201},
        secret_params=secret,
    )
    out["per_page_201"] = {
        "status": r.status_code if r else None,
        "body": str(_body(r))[:200],
    }

    # 3. filters (date + type) and select
    r = await src.get(
        f"{BASE}/works",
        "filter_date_type",
        params={
            "search": q1,
            "per-page": 5,
            "filter": "from_publication_date:2024-01-01,type:article|preprint",
            "select": "id,title,publication_year,type",
        },
        secret_params=secret,
    )
    b = _body(r)
    out["filter_date_type"] = {
        "status": r.status_code if r else None,
        "count": (b.get("meta") or {}).get("count"),
        "types": sorted({w.get("type") for w in b.get("results", [])}),
        "years": sorted({w.get("publication_year") for w in b.get("results", [])}),
    }

    # 4. seed resolution by DOI (arXiv-registered DOI)
    r = await src.get(
        f"{BASE}/works/doi:10.48550/arxiv.1706.03762",
        "lookup_doi",
        params={"select": "id,title,doi,ids"},
        secret_params=secret,
    )
    out["lookup_doi"] = {
        "status": r.status_code if r else None,
        "body": str(_body(r))[:300],
    }

    # 5. references / citations from a q2 result with references
    seed = next((w for w in first_results["q2"] if w.get("referenced_works")), None)
    if seed:
        wid = seed["id"].rsplit("/", 1)[-1]
        refs = seed["referenced_works"]
        out["seed"] = {
            "id": wid,
            "n_referenced_works": len(refs),
            "cited_by_count": seed.get("cited_by_count"),
        }
        sample_ids = "|".join(u.rsplit("/", 1)[-1] for u in refs[:20])
        r = await src.get(
            f"{BASE}/works",
            "refs_batch",
            params={
                "filter": f"openalex:{sample_ids}",
                "per-page": 20,
                "select": "id,title,publication_year,doi,abstract_inverted_index",
            },
            secret_params=secret,
        )
        b = _body(r)
        out["refs_batch"] = {
            "status": r.status_code if r else None,
            "n": len(b.get("results", [])),
            "meta_count": (b.get("meta") or {}).get("count"),
        }
        r = await src.get(
            f"{BASE}/works",
            "cited_by",
            params={
                "filter": f"cites:{wid}",
                "per-page": 20,
                "select": "id,title,publication_year,doi,abstract_inverted_index",
            },
            secret_params=secret,
        )
        b = _body(r)
        out["cited_by"] = {
            "status": r.status_code if r else None,
            "n": len(b.get("results", [])),
            "meta_count": (b.get("meta") or {}).get("count"),
        }
    else:
        out["seed"] = None

    # 6. no-mailto comparison (anonymous pool): one request, compare headers/status
    r = await src.get(
        f"{BASE}/works",
        "anon_no_mailto",
        params={"search": q1, "per-page": 1, "select": "id"},
    )
    out["anon_no_mailto"] = {
        "status": r.status_code if r else None,
        "body": str(_body(r))[:200],
    }

    return out, src.log
