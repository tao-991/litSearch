from __future__ import annotations

import re
from pathlib import Path

import pytest

from litsearch.config import DEFAULT_DATA_DIR, Settings, parse_env_file

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_parse_env_file_handles_comments_quotes_and_export() -> None:
    text = """
# a full-line comment
A=plain
B = spaced value   # inline comment
C="quoted # not a comment"
D='single'
export E=exported
F=
G=   # only a comment
H=has#hash
not a valid line
=no_key
"""
    assert parse_env_file(text) == {
        "A": "plain",
        "B": "spaced value",
        "C": "quoted # not a comment",
        "D": "single",
        "E": "exported",
        "F": "",
        "G": "",
        "H": "has#hash",
    }


def test_env_example_parses_to_empty_values() -> None:
    """Regression: a copied .env.example must not turn comments into values."""
    values = parse_env_file((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))
    assert values  # the file declares variables
    assert all(v == "" for v in values.values()), values


def test_defaults_when_nothing_is_set() -> None:
    s = Settings.from_env(environ={})
    assert s.contact_email is None
    assert s.openalex_api_key is None
    assert s.l1_model is None
    assert s.gs_min_delay == 10.0
    assert s.gs_max_delay == 30.0
    assert s.data_dir == DEFAULT_DATA_DIR


def test_environment_overrides_file_and_empty_does_not(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CONTACT_EMAIL=file@example.org\nS2_API_KEY=from-file\n")
    s = Settings.from_env(
        environ={"CONTACT_EMAIL": "env@example.org", "S2_API_KEY": ""},
        env_file=env_file,
    )
    assert s.contact_email == "env@example.org"
    assert s.s2_api_key == "from-file"


def test_missing_env_file_is_ignored(tmp_path: Path) -> None:
    s = Settings.from_env(environ={}, env_file=tmp_path / "nope.env")
    assert s.contact_email is None


def test_reads_all_fields(tmp_path: Path) -> None:
    s = Settings.from_env(
        environ={
            "CONTACT_EMAIL": "me@example.org",
            "OPENALEX_API_KEY": "oa",
            "S2_API_KEY": "s2",
            "ANTHROPIC_API_KEY": "an",
            "L1_MODEL": "cheap-model",
            "QUERY_MODEL": "strong-model",
            "GS_MIN_DELAY": "1.5",
            "GS_MAX_DELAY": "2.5",
            "HTTPS_PROXY": "http://proxy:8080",
            "DATA_DIR": str(tmp_path / "d"),
        }
    )
    assert (s.contact_email, s.openalex_api_key, s.s2_api_key) == (
        "me@example.org",
        "oa",
        "s2",
    )
    assert s.anthropic_api_key == "an"
    assert (s.l1_model, s.query_model) == ("cheap-model", "strong-model")
    assert (s.gs_min_delay, s.gs_max_delay) == (1.5, 2.5)
    assert s.https_proxy == "http://proxy:8080"
    assert s.data_dir == tmp_path / "d"


def test_secrets_are_not_in_repr() -> None:
    s = Settings.from_env(
        environ={
            "CONTACT_EMAIL": "private@example.org",
            "OPENALEX_API_KEY": "secret-oa",
            "S2_API_KEY": "secret-s2",
            "ANTHROPIC_API_KEY": "secret-an",
            "L1_MODEL": "some-model",
        }
    )
    text = repr(s)
    for secret in ("private@example.org", "secret-oa", "secret-s2", "secret-an"):
        assert secret not in text
    assert "some-model" in text


def test_invalid_number_names_the_variable() -> None:
    with pytest.raises(ValueError, match="GS_MIN_DELAY"):
        Settings.from_env(environ={"GS_MIN_DELAY": "soon"})


def test_min_delay_must_not_exceed_max_delay() -> None:
    with pytest.raises(ValueError, match="GS_MIN_DELAY"):
        Settings.from_env(environ={"GS_MIN_DELAY": "30", "GS_MAX_DELAY": "10"})


def test_anthropic_base_url_is_read_and_kept_out_of_repr() -> None:
    s = Settings.from_env(
        environ={"ANTHROPIC_BASE_URL": " https://relay.example.com/v1 "}
    )
    assert s.anthropic_base_url == "https://relay.example.com/v1"
    assert "relay.example.com" not in repr(s)
    assert Settings.from_env(environ={}).anthropic_base_url is None


@pytest.mark.parametrize("bad", ["relay.example.com", "ftp://relay.example.com", "/v1"])
def test_anthropic_base_url_must_be_an_http_url(bad: str) -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_BASE_URL"):
        Settings.from_env(environ={"ANTHROPIC_BASE_URL": bad})


def test_env_example_lists_every_variable_the_settings_read() -> None:
    """Regression guard for the rule that .env.example stays current."""
    source = (REPO_ROOT / "litsearch" / "config.py").read_text(encoding="utf-8")
    read = set(re.findall(r'_(?:opt|float)\(merged, "([A-Z0-9_]+)"', source))
    assert "ANTHROPIC_BASE_URL" in read  # the pattern really finds variables
    documented = set(
        parse_env_file((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))
    )
    assert read - documented == set(), f"missing from .env.example: {read - documented}"
