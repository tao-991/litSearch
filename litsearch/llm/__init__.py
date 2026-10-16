"""Model access: one client that speaks the Anthropic and the OpenAI chat formats."""

from __future__ import annotations

import httpx

from litsearch import __version__
from litsearch.config import Settings
from litsearch.llm.client import LLMClient, LLMError, LLMReply, detect_format

__all__ = ["LLMClient", "LLMError", "LLMReply", "build_llm", "detect_format"]


def build_llm(settings: Settings, *, timeout: float = 120.0) -> LLMClient:
    """A model client for the configured endpoint.

    The contact e-mail is deliberately not sent in the User-Agent: the model endpoint
    (often a third-party relay) is not a data source and does not need it.
    """
    http = httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": f"litsearch/{__version__}"},
        proxy=settings.https_proxy,
    )
    return LLMClient(
        http,
        api_key=settings.llm_api_key or "",
        base_url=settings.llm_base_url,
        api_format=settings.llm_api_format,
    )
