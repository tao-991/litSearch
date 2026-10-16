"""Where the application keeps its files (settings, database, results, uploads).

It is the folder you start the program from (the project folder), exactly like the
command line, unless ``LITSEARCH_HOME`` points somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "LITSEARCH_HOME"


def app_home() -> Path:
    override = os.environ.get(HOME_ENV, "").strip()
    return Path(override).expanduser() if override else Path.cwd()


def resolve(home: Path, path: Path) -> Path:
    """``path`` made absolute: relative paths are relative to the home folder."""
    return path if path.is_absolute() else home / path
