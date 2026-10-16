"""Reading the opening of a PDF (title, abstract, introduction) as plain text.

Only the first pages are read and only their text is used; the PDF itself is never sent
anywhere. This is enough for a model to understand what a paper is about
(SPEC §3.1); it is not the full-text reading of SPEC §7.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

# pypdf reports harmless font details for many ordinary papers ("fontTools is required
# to fully parse ...") on every page; the text is still extracted, so keep it quiet.
logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_PAGES = 4
MAX_CHARS = 6000
MAX_BYTES = 50 * 1024 * 1024


class PdfError(Exception):
    """The PDF cannot be used; the message says why in plain words."""


@dataclass(frozen=True, slots=True)
class PdfOpening:
    name: str  # file name
    text: str  # whitespace-normalized text of the first pages
    n_pages: int  # pages in the whole document
    pages_read: int
    truncated: bool  # more text was available than the character budget allows


def _normalize(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"-\n(?=[a-z])", "", text)  # join words hyphenated at a line end
    return re.sub(r"\s+", " ", text).strip()


def read_opening(
    path: Path,
    *,
    max_pages: int = MAX_PAGES,
    max_chars: int = MAX_CHARS,
    max_bytes: int = MAX_BYTES,
) -> PdfOpening:
    """Text of the first ``max_pages`` pages, cut to ``max_chars`` characters."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(1024)
    except OSError as exc:
        raise PdfError(f"cannot read {path}: {exc.strerror}") from None
    if b"%PDF-" not in header:
        raise PdfError(f"{path.name} is not a PDF file")
    if size > max_bytes:
        raise PdfError(
            f"{path.name} is too large ({size // (1024 * 1024)} MB; the limit is "
            f"{max_bytes // (1024 * 1024)} MB)"
        )

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfError(f"{path.name} is password protected")
        n_pages = len(reader.pages)
        pieces: list[str] = []
        pages_read = 0
        for page in reader.pages[:max_pages]:
            pieces.append(page.extract_text() or "")
            pages_read += 1
    except PdfError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, OSError, RecursionError):
        raise PdfError(
            f"{path.name} could not be read (damaged or unusual PDF)"
        ) from None

    text = _normalize(" ".join(pieces))
    if not text:
        raise PdfError(
            f"{path.name} has no text on its first pages (a scanned PDF? "
            "it would need OCR, which is not supported)"
        )
    truncated = len(text) > max_chars
    return PdfOpening(
        name=path.name,
        text=text[:max_chars],
        n_pages=n_pages,
        pages_read=pages_read,
        truncated=truncated,
    )
