from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from litsearch.pdf import PdfError, read_opening
from tests.pdf_fixtures import make_pdf


def write(tmp_path: Path, pages: list[list[str]], name: str = "paper.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(make_pdf(pages))
    return path


def test_the_text_of_the_first_pages_is_read_in_order(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        [
            ["Human-in-the-loop agents", "Abstract: we study agents."],
            ["1 Introduction", "Agents write code."],
        ],
    )
    opening = read_opening(path)
    assert opening.name == "paper.pdf"
    assert opening.n_pages == 2 and opening.pages_read == 2
    assert not opening.truncated
    assert opening.text.index("Human-in-the-loop") < opening.text.index("Introduction")
    assert "Abstract: we study agents." in opening.text
    assert "\n" not in opening.text  # whitespace is normalized


def test_only_the_first_pages_are_read(tmp_path: Path) -> None:
    path = write(tmp_path, [[f"Text of page {i}"] for i in range(1, 7)])
    opening = read_opening(path, max_pages=3)
    assert (opening.n_pages, opening.pages_read) == (6, 3)
    assert "page 3" in opening.text and "page 4" not in opening.text


def test_the_character_budget_cuts_the_text_and_says_so(tmp_path: Path) -> None:
    path = write(tmp_path, [["word " * 200]])
    opening = read_opening(path, max_chars=100)
    assert len(opening.text) == 100
    assert opening.truncated
    assert not read_opening(path, max_chars=10_000).truncated


def test_words_hyphenated_at_a_line_end_are_joined(tmp_path: Path) -> None:
    path = write(tmp_path, [["the evalua-", "tion of agents"]])
    assert "evaluation of agents" in read_opening(path).text


def test_parentheses_and_backslashes_in_the_text_survive(tmp_path: Path) -> None:
    path = write(tmp_path, [["Accepted (not) by humans"]])
    assert "Accepted (not) by humans" in read_opening(path).text


def test_a_pdf_without_text_is_reported_as_possibly_scanned(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)
    with pytest.raises(PdfError, match="no text on its first pages"):
        read_opening(path)


def test_a_file_that_is_not_a_pdf_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notes.pdf"
    path.write_text("just some text, not a PDF", encoding="utf-8")
    with pytest.raises(PdfError, match="is not a PDF file"):
        read_opening(path)


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(PdfError, match="cannot read"):
        read_opening(tmp_path / "nope.pdf")


def test_a_too_large_file_is_refused_before_reading(tmp_path: Path) -> None:
    path = write(tmp_path, [["small text"]])
    with pytest.raises(PdfError, match="too large"):
        read_opening(path, max_bytes=10)


def test_a_damaged_pdf_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a real pdf body\n%%EOF\n")
    with pytest.raises(PdfError, match="could not be read"):
        read_opening(path)


def test_a_password_protected_pdf_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "locked.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    with path.open("wb") as handle:
        writer.write(handle)
    with pytest.raises(PdfError, match="password protected"):
        read_opening(path)


def test_pypdf_font_warnings_are_silenced() -> None:
    import logging

    assert logging.getLogger("pypdf").level >= logging.ERROR
