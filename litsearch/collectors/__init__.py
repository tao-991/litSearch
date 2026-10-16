"""Collectors: one polite client per scholarly data source."""

from __future__ import annotations

from typing import Any

import httpx

from litsearch import __version__
from litsearch.collectors.arxiv import ArxivCollector
from litsearch.collectors.base import build_user_agent
from litsearch.collectors.openalex import OpenAlexCollector
from litsearch.collectors.semantic_scholar import SemanticScholarCollector
from litsearch.config import Settings

SOURCE_NAMES = ("openalex", "s2", "arxiv")


def build_client(settings: Settings, *, timeout: float = 30.0) -> httpx.AsyncClient:
    """HTTP client shared by collectors: polite User-Agent, optional proxy."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": build_user_agent(__version__, settings.contact_email)},
        proxy=settings.https_proxy,
    )


def build_collectors(
    client: httpx.AsyncClient,
    settings: Settings,
    sources: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """One collector per requested source name (``openalex``, ``s2``, ``arxiv``)."""
    unknown = [name for name in sources if name not in SOURCE_NAMES]
    if unknown:
        raise ValueError(f"unknown source(s) {unknown}; use {SOURCE_NAMES}")
    makers = {
        "openalex": lambda: OpenAlexCollector(
            client,
            email=settings.contact_email,
            api_key=settings.openalex_api_key,
        ),
        "s2": lambda: SemanticScholarCollector(client, api_key=settings.s2_api_key),
        "arxiv": lambda: ArxivCollector(client),
    }
    return {name: makers[name]() for name in sources}
