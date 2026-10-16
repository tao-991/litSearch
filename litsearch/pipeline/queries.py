"""Query generation (SPEC §3.2): the model reads the project brief and decides which
search queries are needed. There is no fixed number; ``max_queries`` is only a safety
ceiling applied afterwards to absurdly long answers (the least important queries, which
the model lists last, are the ones dropped)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from litsearch.llm import LLMClient, LLMError
from litsearch.llm.cache import get_cached, input_hash, put_cached
from litsearch.llm.jsonutil import extract_json
from litsearch.llm.prompts import JSON_REMINDER, QUERIES_SYSTEM, QUERIES_VERSION

TASK = "queries"
MAX_TOKENS = 4000  # generous: some models spend part of it on hidden reasoning
MAX_QUERY_WORDS = 10
MAX_QUERY_CHARS = 120


class NotCachedError(LookupError):
    """Queries for this project were never generated and a model call is not allowed."""


@dataclass(frozen=True, slots=True)
class GeneratedQuery:
    text: str
    angle: str


@dataclass(frozen=True, slots=True)
class QueryPlan:
    queries: list[GeneratedQuery]
    n_rejected: int  # unusable or duplicate entries the model produced
    n_over_ceiling: int  # valid queries dropped because of ``max_queries``
    from_cache: bool
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


def _clean_query(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    text = " ".join(re.sub('["\u201c\u201d\u2018\u2019`]', " ", raw).split()).strip(
        " ,;."
    )
    if not text or len(text) > MAX_QUERY_CHARS or len(text.split()) > MAX_QUERY_WORDS:
        return None
    return text


def parse_queries(text: str, max_queries: int) -> tuple[list[GeneratedQuery], int, int]:
    """(queries, rejected count, count dropped for the ceiling) from a model reply.

    Raises ``ValueError`` if the reply is not the expected JSON.
    """
    data = extract_json(text)
    items = data.get("queries") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("the reply has no list of queries")
    found: list[GeneratedQuery] = []
    seen: set[str] = set()
    rejected = 0
    for item in items:
        if isinstance(item, dict):
            raw, angle = item.get("query"), item.get("angle")
        else:
            raw, angle = item, ""
        query = _clean_query(raw)
        if query is None or query.lower() in seen:
            rejected += 1
            continue
        seen.add(query.lower())
        found.append(GeneratedQuery(query, " ".join(str(angle or "").split())))
    over = max(0, len(found) - max_queries)
    return found[:max_queries], rejected, over


async def generate_queries(
    llm: LLMClient | None,
    conn: sqlite3.Connection,
    *,
    model: str,
    project: str,
    language: str,
    max_queries: int,
    cache_only: bool = False,
) -> QueryPlan:
    """The queries for ``project`` (cached by input, prompt version and model).

    With ``cache_only`` no model call is made; ``NotCachedError`` is raised on a miss.
    """
    # the ceiling is deliberately not in the prompt: a number there makes the model
    # write that many queries; it is only applied afterwards (so one cached answer
    # serves every ceiling)
    system = QUERIES_SYSTEM.format(language=language)
    user = "Project brief:\n<<<\n" + project.strip() + "\n>>>"
    key = input_hash(system, user)
    cached = get_cached(conn, TASK, key, QUERIES_VERSION, model)
    if cached is not None:
        try:
            queries, rejected, over = parse_queries(cached, max_queries)
            if queries:
                return QueryPlan(queries, rejected, over, True, model)
        except ValueError:
            pass  # a damaged cache entry is treated as a miss
    if cache_only or llm is None:
        raise NotCachedError("queries for this project were not generated yet")

    prompt = user
    last_problem = ""
    for _ in range(2):
        reply = await llm.complete(
            model=model,
            system=system,
            user=prompt,
            max_tokens=MAX_TOKENS,
            json_mode=True,
        )
        try:
            queries, rejected, over = parse_queries(reply.text, max_queries)
        except ValueError as exc:
            last_problem = str(exc)
            prompt = user + "\n\n" + JSON_REMINDER
            continue
        if not queries:
            last_problem = "the reply contained no usable query"
            prompt = user + "\n\n" + JSON_REMINDER
            continue
        put_cached(
            conn,
            TASK,
            key,
            QUERIES_VERSION,
            model,
            reply.text,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )
        return QueryPlan(
            queries,
            rejected,
            over,
            False,
            reply.model,
            reply.input_tokens,
            reply.output_tokens,
        )
    raise LLMError("error", f"the model did not return usable queries ({last_problem})")


def queries_file_text(plan: QueryPlan, project_name: str = "the project") -> str:
    """Plain text for ``queries.txt``: one query per line, the angle as a comment."""
    lines = [
        f"# Search queries for {project_name}.",
        "# Edit freely (delete lines, add your own), then run:",
        "#   litsearch run --queries-file queries.txt",
        "",
    ]
    for q in plan.queries:
        if q.angle:
            lines.append(f"# {q.angle}")
        lines += [q.text, ""]
    return "\n".join(lines)


def write_queries_file(plan: QueryPlan, path: Path, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(queries_file_text(plan), encoding="utf-8")
