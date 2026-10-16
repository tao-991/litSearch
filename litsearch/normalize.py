"""Normalization of identifiers (SPEC §5.2), shared by collectors and de-duplication."""

from __future__ import annotations

import re
import unicodedata

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


_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})


def _fold(text: str) -> str:
    """Lowercase, strip accents, turn every run of non-word characters into a space."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"\W+", " ", stripped.lower()).split())


def normalize_title(title: str | None) -> str:
    """Title reduced to lowercase words, so case, punctuation and hyphens do not matter.

    ``Human-In-The-Loop: Agents`` and ``human in the loop - agents`` become equal.
    """
    return _fold(title) if title else ""


def author_surname(name: str | None) -> str | None:
    """Folded surname from ``First Last``, ``F Last`` or ``Last, First`` style names."""
    if not name or not name.strip():
        return None
    if "," in name:
        tokens = _fold(name.split(",", 1)[0]).split()
    else:
        tokens = _fold(name).split()
        while len(tokens) > 1 and tokens[-1] in _NAME_SUFFIXES:
            tokens.pop()
    return tokens[-1] if tokens else None
