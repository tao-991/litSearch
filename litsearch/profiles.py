"""Size profiles (SPEC §1.3): defaults for how much a run searches and reads."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PROFILE = "quick"


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    max_queries: int  # safety ceiling; the model decides how many queries it needs
    per_query: int  # papers requested per query and source
    rounds: int  # snowball rounds
    max_scored: int  # most papers the model reads
    max_minutes: float  # wall-clock limit
    sources: tuple[str, ...] | None = None  # None means every source


PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile(
            "test",
            max_queries=2,
            per_query=5,
            rounds=0,
            max_scored=5,
            max_minutes=3,
            sources=("arxiv",),
        ),
        Profile(
            "quick",
            max_queries=5,
            per_query=15,
            rounds=1,
            max_scored=150,
            max_minutes=10,
        ),
        Profile("standard", 10, 30, 2, 500, 30),
        Profile("deep", 20, 50, 3, 1500, 60),
    )
}
