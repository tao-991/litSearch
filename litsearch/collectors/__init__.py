"""Collectors: one polite client per scholarly data source."""

from __future__ import annotations

import httpx

from litsearch import __version__
from litsearch.collectors.base import build_user_agent
from litsearch.config import Settings


def build_client(settings: Settings, *, timeout: float = 30.0) -> httpx.AsyncClient:
    """HTTP client shared by collectors: polite User-Agent, optional proxy."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": build_user_agent(__version__, settings.contact_email)},
        proxy=settings.https_proxy,
    )
