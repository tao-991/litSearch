from __future__ import annotations

from pathlib import Path

import pytest

from litsearch import paths


def test_the_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "mine"))
    assert paths.app_home() == tmp_path / "mine"
    monkeypatch.setenv(paths.HOME_ENV, "~/somewhere")
    assert paths.app_home() == Path.home() / "somewhere"


def test_without_an_override_it_is_the_current_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(paths.HOME_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.app_home().resolve() == tmp_path.resolve()


def test_resolve_makes_relative_paths_relative_to_the_home(tmp_path: Path) -> None:
    assert paths.resolve(tmp_path, Path("data")) == tmp_path / "data"
    absolute = Path("/var/elsewhere")
    assert paths.resolve(tmp_path, absolute) == absolute
