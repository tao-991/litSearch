from __future__ import annotations

import asyncio

from litsearch import __version__
from litsearch.collectors import build_client
from litsearch.config import Settings


def user_agent(settings: Settings) -> str:
    async def get() -> str:
        async with build_client(settings) as client:
            return str(client.headers["user-agent"])

    return asyncio.run(get())


def test_user_agent_contains_the_contact_email_when_configured() -> None:
    settings = Settings.from_env(environ={"CONTACT_EMAIL": "me@example.org"})
    assert user_agent(settings) == f"litsearch/{__version__} (mailto:me@example.org)"


def test_user_agent_without_email() -> None:
    assert user_agent(Settings.from_env(environ={})) == f"litsearch/{__version__}"
