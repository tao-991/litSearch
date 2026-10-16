"""Turning a free-text description (and later PDFs) into a project brief."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from litsearch.llm import LLMClient, LLMError
from litsearch.llm.cache import get_cached, input_hash, put_cached
from litsearch.llm.prompts import BRIEF_SYSTEM, BRIEF_VERSION

TASK = "brief"
MAX_TOKENS = 3000  # generous: some models spend part of it on hidden reasoning

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class BriefResult:
    text: str  # Markdown
    from_cache: bool
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


def build_brief_prompt(about: str, excerpts: Sequence[tuple[str, str]] = ()) -> str:
    """The user message: their words, then excerpts of any papers they supplied."""
    parts = ["The user's description:\n<<<\n" + about.strip() + "\n>>>"]
    for index, (name, text) in enumerate(excerpts, start=1):
        header = f"Paper {index} provided by the user ({name}), opening excerpt:"
        parts.append(f"{header}\n<<<\n{text.strip()}\n>>>")
    return "\n\n".join(parts)


def _clean(text: str) -> str:
    text = text.strip()
    fenced = _FENCE.match(text)
    return (fenced.group(1) if fenced else text).strip()


async def make_brief(
    llm: LLMClient,
    conn: sqlite3.Connection,
    *,
    model: str,
    about: str,
    language: str,
    excerpts: Sequence[tuple[str, str]] = (),
) -> BriefResult:
    """Write the brief (cached by input, prompt version and model)."""
    if not about.strip() and not excerpts:
        raise ValueError("nothing to write a brief about")
    system = BRIEF_SYSTEM.format(language=language)
    user = build_brief_prompt(about, excerpts)
    key = input_hash(system, user)
    cached = get_cached(conn, TASK, key, BRIEF_VERSION, model)
    if cached is not None:
        return BriefResult(cached, True, model)
    reply = await llm.complete(
        model=model, system=system, user=user, max_tokens=MAX_TOKENS
    )
    text = _clean(reply.text)
    if not text:
        raise LLMError(
            "error", "the model returned an empty brief (it may have run out of tokens)"
        )
    put_cached(
        conn,
        TASK,
        key,
        BRIEF_VERSION,
        model,
        text,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )
    return BriefResult(
        text, False, reply.model, reply.input_tokens, reply.output_tokens
    )
