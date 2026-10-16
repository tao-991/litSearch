"""Size profiles (SPEC §1.3): defaults for how much a run searches and reads."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PROFILE = "quick"


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    n_queries: int  # about how many queries query generation should produce
    per_query: int  # papers requested per query and source
    rounds: int  # snowball rounds
    max_scored: int  # most papers the model reads
    max_minutes: float  # wall-clock limit


PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile(
            "quick", n_queries=8, per_query=15, rounds=1, max_scored=150, max_minutes=10
        ),
        Profile("standard", 20, 30, 2, 500, 30),
        Profile("deep", 40, 50, 3, 1500, 60),
    )
}
