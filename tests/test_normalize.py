from __future__ import annotations

import pytest

from litsearch.normalize import (
    arxiv_id_from_doi,
    normalize_arxiv_id,
    normalize_doi,
    openalex_short_id,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1145/3630106.3658984", "10.1145/3630106.3658984"),
        ("https://doi.org/10.1109/MSR66628.2025.00112", "10.1109/msr66628.2025.00112"),
        ("http://dx.doi.org/10.1000/ABC", "10.1000/abc"),
        ("doi:10.1000/xyz", "10.1000/xyz"),
        ("DOI:10.1000/xyz", "10.1000/xyz"),
        ("  https://doi.org/10.1000/xyz  ", "10.1000/xyz"),
        ("https://doi.org/10.48550/arxiv.2601.21379", "10.48550/arxiv.2601.21379"),
        ("", None),
        (None, None),
        ("not a doi", None),
        ("https://doi.org/", None),
        ("10.1000", None),
    ],
)
def test_normalize_doi(raw: str | None, expected: str | None) -> None:
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2601.21379", "2601.21379"),
        ("2601.21379v1", "2601.21379"),
        ("1706.03762v7", "1706.03762"),
        ("arXiv:2601.21379v2", "2601.21379"),
        ("ARXIV:2601.21379", "2601.21379"),
        ("http://arxiv.org/abs/2601.21379v1", "2601.21379"),
        ("https://arxiv.org/abs/2601.21379", "2601.21379"),
        ("https://arxiv.org/pdf/2601.21379v3", "2601.21379"),
        ("https://arxiv.org/pdf/2601.21379v3.pdf", "2601.21379"),
        ("https://arxiv.org/pdf/2601.21379.pdf", "2601.21379"),
        ("http://export.arxiv.org/abs/2506.11009", "2506.11009"),
        ("2506.1101", "2506.1101"),
        ("cs/0112017", "cs/0112017"),
        ("cs/0112017v1", "cs/0112017"),
        ("http://arxiv.org/abs/hep-th/9901001v2", "hep-th/9901001"),
        ("math.GT/0309136", "math.GT/0309136"),
        ("", None),
        (None, None),
        ("hello", None),
        ("12345", None),
        ("https://example.org/abs/2601.21379", None),
    ],
)
def test_normalize_arxiv_id(raw: str | None, expected: str | None) -> None:
    assert normalize_arxiv_id(raw) == expected


@pytest.mark.parametrize(
    ("doi", "expected"),
    [
        ("10.48550/arxiv.2601.21379", "2601.21379"),
        ("https://doi.org/10.48550/arXiv.2601.21379", "2601.21379"),
        ("10.48550/arxiv.2601.21379v2", "2601.21379"),
        ("10.1109/msr66628.2025.00112", None),
        ("10.48550/arxiv.garbage", None),
        (None, None),
    ],
)
def test_arxiv_id_from_doi(doi: str | None, expected: str | None) -> None:
    assert arxiv_id_from_doi(doi) == expected


def test_openalex_short_id() -> None:
    assert openalex_short_id("https://openalex.org/W4411271192") == "W4411271192"
    assert openalex_short_id("W42") == "W42"
    assert openalex_short_id("https://openalex.org/W42/") == "W42"
    assert openalex_short_id(None) is None
    assert openalex_short_id("") is None
