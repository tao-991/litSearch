"""Editing a ``.env`` file in place without touching anything else in it.

Used by the settings page. Comments, blank lines, order and unknown variables are kept;
values are never logged or returned.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

# The only variables the settings page may change.
EDITABLE = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_API_FORMAT",
    "L1_MODEL",
    "QUERY_MODEL",
    "SUMMARY_LANGUAGE",
    "CONTACT_EMAIL",
    "OPENALEX_API_KEY",
    "S2_API_KEY",
    "HTTPS_PROXY",
)
# Never shown back to the user (only whether they are set).
SECRET = frozenset({"LLM_API_KEY", "OPENALEX_API_KEY", "S2_API_KEY"})


def _format(value: str) -> str:
    """A value as it must be written so that ``parse_env_file`` reads it back."""
    if value == "" or not re.search(r"[\s#\"']", value):
        return value
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    raise ValueError("a value cannot contain both kinds of quotation marks")


def update_env_file(path: Path, updates: Mapping[str, str | None]) -> None:
    """Set the given variables (``None`` or empty clears one); keep the rest as is."""
    for key, value in updates.items():
        if key not in EDITABLE:
            raise ValueError(f"{key} cannot be changed here")
        if value is not None and ("\n" in value or "\r" in value):
            raise ValueError(f"{key}: a value cannot contain a line break")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[i] = f"{key}={_format(remaining.pop(key) or '')}"
    for key, value in remaining.items():
        lines.append(f"{key}={_format(value or '')}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)  # the file may hold API keys
    temp.replace(path)
