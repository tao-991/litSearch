"""A scripted stand-in for the model client."""

from __future__ import annotations

from typing import Any

from litsearch.llm import LLMError, LLMReply


class FakeLLM:
    """Replies come from a list: text, an ``LLMReply``, or an exception to raise."""

    def __init__(
        self, replies: list[Any] | None = None, model: str = "fake-model"
    ) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []
        self._model = model

    def format_for(self, model: str) -> str:
        return "openai"

    async def complete(self, **kwargs: Any) -> LLMReply:
        self.calls.append(kwargs)
        if not self.replies:
            raise AssertionError("unexpected model call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, LLMReply):
            return reply
        return LLMReply(
            text=str(reply),
            model=self._model,
            input_tokens=100,
            output_tokens=50,
            finish_reason="stop",
        )

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None


__all__ = ["FakeLLM", "LLMError"]
