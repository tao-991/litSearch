"""Cache of model answers (CLAUDE.md: LLM results are cached by input hash, prompt
version and model), so the same question is never paid for twice."""

from __future__ import annotations

import hashlib
import sqlite3


def input_hash(*parts: str) -> str:
    """Stable hash of everything that determines a model's answer."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def get_cached(
    conn: sqlite3.Connection, task: str, key: str, prompt_version: str, model: str
) -> str | None:
    row = conn.execute(
        "SELECT output FROM llm_cache "
        "WHERE task = ? AND input_hash = ? AND prompt_version = ? AND model = ?",
        (task, key, prompt_version, model),
    ).fetchone()
    return None if row is None else str(row["output"])


def put_cached(
    conn: sqlite3.Connection,
    task: str,
    key: str,
    prompt_version: str,
    model: str,
    output: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO llm_cache (task, input_hash, prompt_version, model, output, "
            "input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task, input_hash, prompt_version, model) DO UPDATE SET "
            "output = excluded.output, input_tokens = excluded.input_tokens, "
            "output_tokens = excluded.output_tokens",
            (task, key, prompt_version, model, output, input_tokens, output_tokens),
        )
