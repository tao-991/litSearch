"""Normalization of identifiers (SPEC §5.2), shared by collectors and de-duplication."""

from __future__ import annotations

import re

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi.org/",
    "doi:",
)
_ARXIV_URL = re.compile(
    r"^(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/", re.IGNORECASE
)
_ARXIV_NEW = re.compile(r"^\d{4}\.\d{4,5}$")
_ARXIV_OLD = re.compile(r"^[a-z][a-z\-]*(?:\.[A-Za-z]{2})?/\d{7}$")
_ARXIV_VERSION = re.compile(r"v\d+$")
_ARXIV_DOI_PREFIX = "10.48550/arxiv."


def normalize_doi(value: str | None) -> str | None:
    """Lowercase DOI without any URL or ``doi:`` prefix, or ``None`` if not a DOI."""
    if not value:
        return None
    doi = value.strip()
    lowered = doi.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    doi = doi.strip().lower()
    return doi if doi.startswith("10.") and "/" in doi else None


def normalize_arxiv_id(value: str | None) -> str | None:
    """arXiv ID without URL prefix, ``arXiv:`` prefix, ``.pdf`` suffix or version.

    Handles new-style (``2601.21379``) and old-style (``cs/0112017``) identifiers.
    Returns ``None`` if the value does not look like an arXiv ID.
    """
    if not value:
        return None
    text = value.strip()
    text = _ARXIV_URL.sub("", text)
    if text.lower().startswith("arxiv:"):
        text = text[len("arxiv:") :]
    text = text.removesuffix(".pdf")
    text = _ARXIV_VERSION.sub("", text)
    if _ARXIV_NEW.match(text) or _ARXIV_OLD.match(text):
        return text
    return None


def arxiv_id_from_doi(doi: str | None) -> str | None:
    """arXiv ID encoded in an arXiv-registered DOI (``10.48550/arxiv.<id>``)."""
    normalized = normalize_doi(doi)
    if normalized and normalized.startswith(_ARXIV_DOI_PREFIX):
        return normalize_arxiv_id(normalized[len(_ARXIV_DOI_PREFIX) :])
    return None


def openalex_short_id(value: str | None) -> str | None:
    """``https://openalex.org/W123`` -> ``W123``."""
    if not value:
        return None
    short = value.strip().rstrip("/").rsplit("/", 1)[-1]
    return short or None
