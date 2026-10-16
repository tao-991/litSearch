"""Phase 0 spike: arXiv API (Atom). Rule under test: >= 3 s between requests."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import httpx
from _common import N_PER_QUERY, Source, save_sample

BASE = "https://export.arxiv.org/api/query"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
    "ax": "http://arxiv.org/schemas/atom",
}

# Hand-converted arXiv query syntax for the three shared topics.
ARXIV_QUERIES = {
    "q1": 'all:"AI-generated code" AND (all:trust OR all:acceptance)',
    "q2": 'all:"coding agent" AND all:"pull request"',
    "q3": 'all:"code review" AND (all:explanation OR all:explain) AND all:LLM',
}


def parse_feed(xml_text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = ET.fromstring(xml_text)
    meta = {
        "totalResults": (root.findtext("os:totalResults", namespaces=NS)),
        "startIndex": root.findtext("os:startIndex", namespaces=NS),
        "itemsPerPage": root.findtext("os:itemsPerPage", namespaces=NS),
    }
    entries = []
    for e in root.findall("a:entry", NS):
        links = {
            (l.get("title") or l.get("rel")): l.get("href")
            for l in e.findall("a:link", NS)
        }
        entries.append(
            {
                "id": e.findtext("a:id", namespaces=NS),
                "title": " ".join((e.findtext("a:title", namespaces=NS) or "").split()),
                "summary": " ".join(
                    (e.findtext("a:summary", namespaces=NS) or "").split()
                ),
                "authors": [
                    a.findtext("a:name", namespaces=NS)
                    for a in e.findall("a:author", NS)
                ],
                "published": e.findtext("a:published", namespaces=NS),
                "updated": e.findtext("a:updated", namespaces=NS),
                "doi": e.findtext("ax:doi", namespaces=NS),
                "journal_ref": e.findtext("ax:journal_ref", namespaces=NS),
                "comment": e.findtext("ax:comment", namespaces=NS),
                "primary_category": (
                    e.find("ax:primary_category", NS).get("term")
                    if e.find("ax:primary_category", NS) is not None
                    else None
                ),
                "links": links,
            }
        )
    return meta, entries


async def run(client: httpx.AsyncClient) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    src = Source("arxiv", client, min_interval=3.0)
    out: dict[str, Any] = {"auth": "none"}
    first: dict[str, list[dict[str, Any]]] = {}

    for qid, q in ARXIV_QUERIES.items():
        r = await src.get(
            BASE,
            f"search:{qid}",
            params={
                "search_query": q,
                "start": 0,
                "max_results": N_PER_QUERY,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        if r is None or r.status_code != 200:
            out[f"search_{qid}"] = {
                "status": r.status_code if r else None,
                "body": (r.text[:200] if r else "failed/skipped"),
            }
            continue
        save_sample(f"arxiv_search_{qid}.xml", r.text)
        meta, entries = parse_feed(r.text)
        first[qid] = entries
        n = len(entries)
        out[f"search_{qid}"] = {
            "status": r.status_code,
            "query": q,
            "meta": meta,
            "n": n,
            "abstract": sum(1 for e in entries if e["summary"]),
            "doi": sum(1 for e in entries if e["doi"]),
            "journal_ref": sum(1 for e in entries if e["journal_ref"]),
            "pdf_link": sum(1 for e in entries if "pdf" in e["links"]),
        }
        if qid == "q1" and entries:
            out["entry_example"] = {
                k: v for k, v in entries[0].items() if k != "summary"
            }
            out["id_format_example"] = entries[0]["id"]

    # paging: start=20 on q1
    r = await src.get(
        BASE,
        "page2",
        params={
            "search_query": ARXIV_QUERIES["q1"],
            "start": N_PER_QUERY,
            "max_results": N_PER_QUERY,
            "sortBy": "relevance",
            "sortOrder": "descending",
        },
    )
    if r is not None and r.status_code == 200:
        meta, entries = parse_feed(r.text)
        ids1 = {e["id"] for e in first.get("q1", [])}
        out["page2"] = {
            "meta": meta,
            "n": len(entries),
            "overlap_with_page1": len(ids1 & {e["id"] for e in entries}),
        }
    else:
        out["page2"] = {"status": r.status_code if r else None}

    # seed resolution by arXiv id
    r = await src.get(BASE, "id_list", params={"id_list": "1706.03762"})
    if r is not None and r.status_code == 200:
        meta, entries = parse_feed(r.text)
        out["id_list"] = {
            "n": len(entries),
            "id": entries[0]["id"] if entries else None,
            "doi": entries[0]["doi"] if entries else None,
        }
    else:
        out["id_list"] = {"status": r.status_code if r else None}

    # search with date filter
    r = await src.get(
        BASE,
        "date_filter",
        params={
            "search_query": f"{ARXIV_QUERIES['q2']} AND submittedDate:[202401010000 TO 202612312359]",
            "max_results": 5,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        },
    )
    if r is not None and r.status_code == 200:
        meta, entries = parse_feed(r.text)
        out["date_filter"] = {
            "meta": meta,
            "n": len(entries),
            "published": [e["published"] for e in entries],
        }
    else:
        out["date_filter"] = {"status": r.status_code if r else None}
    return out, src.log
