"""Runtime configuration from environment variables and an optional ``.env`` file.

Everything is optional: a missing value means "feature not configured", never a crash.
Secrets (keys, contact e-mail) are excluded from ``repr`` so they cannot leak into logs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path("./data")
DEFAULT_GS_MIN_DELAY = 10.0
DEFAULT_GS_MAX_DELAY = 30.0


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines.

    Supports blank lines, ``#`` comment lines, an optional ``export`` prefix, quoted
    values, and inline comments in unquoted values (a ``#`` preceded by whitespace).
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        values[key] = _clean_value(value.strip())
    return values


def _clean_value(value: str) -> str:
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:] if end == -1 else value[1:end]
    for i, ch in enumerate(value):
        if ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
    return value


def _opt(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    value = _opt(env, name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {value!r}") from None


@dataclass(frozen=True, slots=True)
class Settings:
    """All tunable parameters that come from the environment."""

    contact_email: str | None = field(default=None, repr=False)
    openalex_api_key: str | None = field(default=None, repr=False)
    s2_api_key: str | None = field(default=None, repr=False)
    anthropic_api_key: str | None = field(default=None, repr=False)
    anthropic_base_url: str | None = field(default=None, repr=False)
    l1_model: str | None = None
    query_model: str | None = None
    gs_min_delay: float = DEFAULT_GS_MIN_DELAY
    gs_max_delay: float = DEFAULT_GS_MAX_DELAY
    https_proxy: str | None = None
    data_dir: Path = DEFAULT_DATA_DIR

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        env_file: Path | None = None,
    ) -> Settings:
        """Build settings from ``env_file`` (if it exists) overlaid by ``environ``.

        Real environment variables win over the file; empty values count as unset.
        ``environ`` defaults to ``os.environ``.
        """
        merged: dict[str, str] = {}
        if env_file is not None and env_file.is_file():
            merged.update(parse_env_file(env_file.read_text(encoding="utf-8")))
        source = os.environ if environ is None else environ
        merged.update({k: v for k, v in source.items() if v.strip()})

        gs_min = _float(merged, "GS_MIN_DELAY", DEFAULT_GS_MIN_DELAY)
        gs_max = _float(merged, "GS_MAX_DELAY", DEFAULT_GS_MAX_DELAY)
        if gs_min < 0 or gs_min > gs_max:
            raise ValueError(
                "GS_MIN_DELAY must be >= 0 and <= GS_MAX_DELAY "
                f"(got {gs_min} and {gs_max})"
            )
        base_url = _opt(merged, "ANTHROPIC_BASE_URL")
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            raise ValueError("ANTHROPIC_BASE_URL must start with http:// or https://")
        data_dir = _opt(merged, "DATA_DIR")
        return cls(
            contact_email=_opt(merged, "CONTACT_EMAIL"),
            openalex_api_key=_opt(merged, "OPENALEX_API_KEY"),
            s2_api_key=_opt(merged, "S2_API_KEY"),
            anthropic_api_key=_opt(merged, "ANTHROPIC_API_KEY"),
            anthropic_base_url=base_url,
            l1_model=_opt(merged, "L1_MODEL"),
            query_model=_opt(merged, "QUERY_MODEL"),
            gs_min_delay=gs_min,
            gs_max_delay=gs_max,
            https_proxy=_opt(merged, "HTTPS_PROXY"),
            data_dir=Path(data_dir).expanduser() if data_dir else DEFAULT_DATA_DIR,
        )
