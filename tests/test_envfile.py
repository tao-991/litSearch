from __future__ import annotations

import stat
from pathlib import Path

import pytest

from litsearch.config import Settings, parse_env_file
from litsearch.envfile import EDITABLE, SECRET, update_env_file


def read(path: Path) -> dict[str, str]:
    return parse_env_file(path.read_text(encoding="utf-8"))


def test_values_are_replaced_in_place_and_everything_else_is_kept(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# my settings\nCONTACT_EMAIL=old@example.org\n\nL1_MODEL=old-model  # note\n"
        "UNKNOWN=keep me\nexport LLM_API_KEY=old-key\n",
        encoding="utf-8",
    )
    update_env_file(path, {"L1_MODEL": "gpt-4.1-nano", "LLM_API_KEY": "new-key"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# my settings" and lines[2] == ""
    assert "L1_MODEL=gpt-4.1-nano" in lines and "LLM_API_KEY=new-key" in lines
    assert "UNKNOWN=keep me" in lines
    assert "CONTACT_EMAIL=old@example.org" in lines
    values = read(path)
    assert values["L1_MODEL"] == "gpt-4.1-nano" and values["LLM_API_KEY"] == "new-key"


def test_missing_variables_are_appended_and_a_missing_file_is_created(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sub" / ".env"
    update_env_file(path, {"QUERY_MODEL": "gpt-6.1-sol", "SUMMARY_LANGUAGE": "English"})
    assert read(path) == {"QUERY_MODEL": "gpt-6.1-sol", "SUMMARY_LANGUAGE": "English"}
    update_env_file(path, {"L1_MODEL": "x"})
    assert read(path)["L1_MODEL"] == "x" and read(path)["QUERY_MODEL"] == "gpt-6.1-sol"


def test_none_and_empty_clear_a_value(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "S2_API_KEY=abc\nLLM_BASE_URL=https://x.example\n", encoding="utf-8"
    )
    update_env_file(path, {"S2_API_KEY": None, "LLM_BASE_URL": ""})
    assert read(path) == {"S2_API_KEY": "", "LLM_BASE_URL": ""}


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "with space",
        "has # hash",
        'say "hi"',
        "it's",
        "a=b",
        "https://r.example/v1",
    ],
)
def test_values_survive_a_round_trip_through_the_parser(
    tmp_path: Path, value: str
) -> None:
    path = tmp_path / ".env"
    update_env_file(path, {"SUMMARY_LANGUAGE": value})
    assert read(path)["SUMMARY_LANGUAGE"] == value
    assert Settings.from_env(environ={}, env_file=path).summary_language == value


def test_unwritable_values_and_unknown_variables_are_refused(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    with pytest.raises(ValueError, match="both kinds"):
        update_env_file(path, {"SUMMARY_LANGUAGE": "a \" and ' b"})
    with pytest.raises(ValueError, match="line break"):
        update_env_file(path, {"L1_MODEL": "a\nLLM_API_KEY=stolen"})
    with pytest.raises(ValueError, match="cannot be changed"):
        update_env_file(path, {"PATH": "/evil"})
    assert not path.exists()  # nothing was written


def test_the_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    update_env_file(path, {"LLM_API_KEY": "secret"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secrets_are_a_subset_of_the_editable_variables() -> None:
    assert SECRET <= set(EDITABLE)
