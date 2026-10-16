"""Reading JSON out of a model's reply (it may add a code fence or some prose)."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """The first JSON object or array found in ``text``.

    Raises ``ValueError`` if there is none or it does not parse.
    """
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except ValueError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = candidate.find(opener), candidate.rfind(closer)
            if 0 <= start < end:
                try:
                    return json.loads(candidate[start : end + 1])
                except ValueError:
                    continue
    raise ValueError("no valid JSON found in the model's reply")
