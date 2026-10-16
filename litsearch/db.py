"""SQLite storage: connection setup, schema (SPEC §5.1) and a few basic helpers.

Design notes
- ``PRAGMA user_version`` holds the schema version; ``init_schema`` is idempotent.
- ``papers.doi`` and ``papers.arxiv_id`` hold *normalized* values (see SPEC §5.2) and
  are unique when present, so the database itself rejects duplicate papers.
- ``scores`` is the LLM result cache: its primary key is the cache key
  (paper, project, input hash, prompt version, model).
- ``paper_records`` keeps every source's raw record so merged papers retain all
  versions and sources.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3

RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

_SCHEMA_V1 = f"""
BEGIN;

CREATE TABLE runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_hash  TEXT NOT NULL,
    params        TEXT NOT NULL DEFAULT '{{}}',
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    started_at    TEXT NOT NULL DEFAULT ({_NOW}),
    finished_at   TEXT
);

-- Counts per query and source are derived from hits / scores, not stored here.
CREATE TABLE queries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    text    TEXT NOT NULL,
    origin  TEXT NOT NULL CHECK (origin IN ('llm', 'user')),
    UNIQUE (run_id, text)
);

CREATE TABLE papers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    abstract        TEXT,
    authors         TEXT NOT NULL DEFAULT '[]',
    year            INTEGER,
    date            TEXT,
    venue           TEXT,
    doi             TEXT,
    arxiv_id        TEXT,
    openalex_id     TEXT,
    s2_id           TEXT,
    citation_count  INTEGER,
    links           TEXT NOT NULL DEFAULT '{{}}',
    oa_pdf_url      TEXT,
    state           TEXT NOT NULL DEFAULT 'candidate',
    created_at      TEXT NOT NULL DEFAULT ({_NOW})
);
CREATE UNIQUE INDEX papers_doi_uq
    ON papers(doi) WHERE doi IS NOT NULL;
CREATE UNIQUE INDEX papers_arxiv_uq
    ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE UNIQUE INDEX papers_openalex_uq
    ON papers(openalex_id) WHERE openalex_id IS NOT NULL;
CREATE UNIQUE INDEX papers_s2_uq
    ON papers(s2_id) WHERE s2_id IS NOT NULL;

CREATE TABLE paper_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    raw         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL DEFAULT ({_NOW}),
    UNIQUE (source, source_id)
);
CREATE INDEX paper_records_paper_idx ON paper_records(paper_id);

-- query_id is NULL for papers that came from snowballing rather than a query.
CREATE TABLE hits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    query_id  INTEGER REFERENCES queries(id) ON DELETE CASCADE,
    source    TEXT NOT NULL,
    paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    rank      INTEGER,
    round     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX hits_run_paper_idx ON hits(run_id, paper_id);
CREATE INDEX hits_paper_idx     ON hits(paper_id);
CREATE INDEX hits_query_idx     ON hits(query_id);

CREATE TABLE scores (
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    project_hash    TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    model           TEXT NOT NULL,
    score           REAL NOT NULL,
    reason          TEXT,
    summary         TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT ({_NOW}),
    PRIMARY KEY (paper_id, project_hash, input_hash, prompt_version, model)
);

CREATE TABLE edges (
    src_paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    dst_paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL DEFAULT 'cites' CHECK (kind IN ('cites')),
    PRIMARY KEY (src_paper_id, dst_paper_id, kind)
);

CREATE TABLE source_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN ('ok', 'blocked', 'rate_limited', 'error', 'skipped')),
    n_requests  INTEGER NOT NULL DEFAULT 0,
    n_results   INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    UNIQUE (run_id, source)
);

PRAGMA user_version = 1;
COMMIT;
"""


# v2: named runs, per-paper publication status, the stored form of every source record
# (so a later run can rebuild the de-duplication state) and the fetch log that makes
# runs resumable. "done" or the failure kind of one (query, source, variant) fetch.
_MIGRATION_V2 = """
BEGIN;

ALTER TABLE runs ADD COLUMN name TEXT;
CREATE UNIQUE INDEX runs_name_uq ON runs(name) WHERE name IS NOT NULL;

ALTER TABLE papers ADD COLUMN publication_status TEXT;
ALTER TABLE papers ADD COLUMN work_type TEXT;

ALTER TABLE paper_records ADD COLUMN meta TEXT;

CREATE TABLE fetches (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    query_id  INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    source    TEXT NOT NULL,
    variant   TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL
              CHECK (status IN ('done', 'blocked', 'rate_limited', 'error', 'skipped')),
    n_results INTEGER NOT NULL DEFAULT 0,
    error     TEXT,
    UNIQUE (run_id, query_id, source, variant)
);

PRAGMA user_version = 2;
COMMIT;
"""


# v3: cache of model answers, keyed by (task, input hash, prompt version, model), so the
# same input never costs a second model call (CLAUDE.md: LLM results are always cached).
_MIGRATION_V3 = """
BEGIN;

CREATE TABLE llm_cache (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task           TEXT NOT NULL,
    input_hash     TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    output         TEXT NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (task, input_hash, prompt_version, model)
);

PRAGMA user_version = 3;
COMMIT;
"""


class SchemaError(RuntimeError):
    """The database was created by a newer version of litsearch."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on and rows addressable by column name.

    ``path`` may be ``":memory:"``; otherwise missing parent directories are created.
    """
    in_memory = str(path) == ":memory:"
    if not in_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not in_memory:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the schema if needed. Safe to call repeatedly."""
    version = schema_version(conn)
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {version} is newer than this litsearch "
            f"supports ({SCHEMA_VERSION}); upgrade litsearch"
        )
    if version < 1:
        conn.executescript(_SCHEMA_V1)
        version = 1
    if version < 2:
        conn.executescript(_MIGRATION_V2)
        version = 2
    if version < 3:
        conn.executescript(_MIGRATION_V3)


def dumps_json(value: Any) -> str:
    """Serialize for a JSON text column (keeps non-ASCII text readable)."""
    return json.dumps(value, ensure_ascii=False)


def loads_json(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def create_run(
    conn: sqlite3.Connection,
    project_hash: str,
    params: Mapping[str, Any] | None = None,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (project_hash, params) VALUES (?, ?)",
            (project_hash, dumps_json(dict(params or {}))),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return row


def set_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    """Update a run's status; terminal statuses also stamp ``finished_at``."""
    finished = _NOW if status in RUN_TERMINAL_STATUSES else "NULL"
    with conn:
        conn.execute(
            f"UPDATE runs SET status = ?, finished_at = {finished} WHERE id = ?",
            (status, run_id),
        )
