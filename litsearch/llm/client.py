"""Chat-completion client for the Anthropic message format and the OpenAI chat format.

Both formats go through the same endpoint root (an official API or an API relay), chosen
per model: names starting with ``claude`` use the Anthropic format
(``POST <root>/v1/messages``), everything else the OpenAI format
(``POST <root>/v1/chat/completions``), unless ``api_format`` forces one. A relay that
offers many vendors' models then works with a single key and base URL.

Requests are polite in the same way as the data-source collectors (retries with
backoff, circuit breaker). The API key is only ever sent in a request header and is
masked in every error message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from litsearch.collectors.base import CollectorError, HttpCollector, SourceStatus

OFFICIAL_BASE = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True, slots=True)
class LLMReply:
    text: str
    model: str  # the model the endpoint says it used
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: (
        str | None
    )  # "end_turn"/"stop" normally, "max_tokens"/"length" if cut


class LLMError(Exception):
    """A model request failed. ``kind`` is one of the values below."""

    KINDS = ("auth", "model", "rate_limited", "error", "skipped")

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def detect_format(model: str, setting: str = "auto") -> str:
    """``"anthropic"`` or ``"openai"`` for a model name (or the forced setting)."""
    if setting in ("anthropic", "openai"):
        return setting
    return "anthropic" if model.strip().lower().startswith("claude") else "openai"


class LLMClient(HttpCollector):
    name = "llm"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str | None = None,
        api_format: str = "auto",
        min_interval: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, min_interval=min_interval, **kwargs)
        self._key = api_key
        self._base_url = base_url
        self._api_format = api_format

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ helpers

    def _root(self, fmt: str) -> str:
        base = self._base_url or OFFICIAL_BASE[fmt]
        return base.rstrip("/").removesuffix("/v1")

    def _mask(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def format_for(self, model: str) -> str:
        return detect_format(model, self._api_format)

    def _error(self, exc: CollectorError) -> LLMError:
        detail = f" {self._mask(exc.detail)}" if exc.detail else ""
        if exc.status is SourceStatus.BLOCKED:
            return LLMError(
                "auth", f"the endpoint refused the key (HTTP {exc.http_status}){detail}"
            )
        if exc.status is SourceStatus.RATE_LIMITED:
            return LLMError("rate_limited", self._mask(exc.message))
        if exc.status is SourceStatus.SKIPPED:
            return LLMError("skipped", self._mask(exc.message))
        if exc.http_status in (400, 404):
            return LLMError(
                "model", f"request rejected (HTTP {exc.http_status}){detail}"
            )
        return LLMError("error", self._mask(exc.message) + detail)

    # ------------------------------------------------------------------- requests

    async def complete(
        self,
        *,
        model: str,
        user: str,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> LLMReply:
        """One question, one answer.

        ``json_mode`` only has an effect in the OpenAI format; in the Anthropic
        format ask for JSON in the prompt.
        """
        if not self._key:
            raise LLMError("auth", "no API key configured (LLM_API_KEY)")
        fmt = self.format_for(model)
        root = self._root(fmt)
        if fmt == "anthropic":
            url = f"{root}/v1/messages"
            headers = {"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION}
            body: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": user}],
            }
            if system:
                body["system"] = system
        else:
            url = f"{root}/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self._key}"}
            messages = [{"role": "system", "content": system}] if system else []
            messages.append({"role": "user", "content": user})
            body = {"model": model, "max_tokens": max_tokens, "messages": messages}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
        if temperature is not None:
            body["temperature"] = temperature

        try:
            response = await self._request("POST", url, headers=headers, json=body)
        except CollectorError as exc:
            if (
                fmt == "openai"
                and exc.http_status == 400
                and "max_completion_tokens" in (exc.detail or "")
                and "max_tokens" in body
            ):
                # newer OpenAI models want the other name for the same limit
                body["max_completion_tokens"] = body.pop("max_tokens")
                try:
                    response = await self._request(
                        "POST", url, headers=headers, json=body
                    )
                except CollectorError as again:
                    raise self._error(again) from None
            else:
                raise self._error(exc) from None
        return self._parse(fmt, response, model)

    def _parse(self, fmt: str, response: httpx.Response, model: str) -> LLMReply:
        try:
            data = response.json()
        except ValueError:
            raise LLMError("error", "the endpoint did not answer with JSON") from None
        try:
            if fmt == "anthropic":
                text = "".join(
                    block.get("text", "")
                    for block in data.get("content") or []
                    if block.get("type") == "text"
                )
                usage = data.get("usage") or {}
                return LLMReply(
                    text=text,
                    model=data.get("model") or model,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    finish_reason=data.get("stop_reason"),
                )
            choice = (data.get("choices") or [{}])[0]
            usage = data.get("usage") or {}
            return LLMReply(
                text=(choice.get("message") or {}).get("content") or "",
                model=data.get("model") or model,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                finish_reason=choice.get("finish_reason"),
            )
        except (AttributeError, TypeError, IndexError):
            raise LLMError(
                "error", "unexpected answer format from the endpoint"
            ) from None

    async def list_models(self) -> list[str]:
        """Model names the endpoint offers (empty if it has no model list)."""
        root = self._root("openai" if self._api_format == "openai" else "anthropic")
        headers = {
            "Authorization": f"Bearer {self._key}",
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        try:
            response = await self._request("GET", f"{root}/v1/models", headers=headers)
            data = response.json().get("data") or []
        except (CollectorError, ValueError, AttributeError):
            return []
        return sorted(str(m["id"]) for m in data if isinstance(m, dict) and m.get("id"))
