from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from litsearch.llm import LLMClient, LLMError, detect_format
from tests.collectors.helpers import FakeTime, Server, json_response

KEY = "sk-secret-key-123"


def make(server: Server, **kwargs: Any) -> LLMClient:
    ft = FakeTime()
    options: dict[str, Any] = {"api_key": KEY, "base_url": "https://relay.example.com"}
    options.update(kwargs)
    return LLMClient(server.client(), sleep=ft.sleep, clock=ft.clock, **options)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def sent(server: Server, index: int = 0) -> tuple[httpx.Request, dict[str, Any]]:
    request = server.requests[index]
    return request, json.loads(request.content)


ANTHROPIC_REPLY = {
    "model": "claude-haiku-4-5-20251001",
    "content": [{"type": "text", "text": "OK"}],
    "usage": {"input_tokens": 13, "output_tokens": 1},
    "stop_reason": "end_turn",
}
OPENAI_REPLY = {
    "model": "gpt-5-nano-2025-08-07",
    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 13, "completion_tokens": 4},
}


# ------------------------------------------------------------------------ formats


@pytest.mark.parametrize(
    ("model", "setting", "expected"),
    [
        ("claude-haiku-4-5-20251001", "auto", "anthropic"),
        ("Claude-Sonnet-5-5", "auto", "anthropic"),
        ("gpt-5-nano", "auto", "openai"),
        ("gemini-3.5-flash-lite", "auto", "openai"),
        ("deepseek-v4-flash", "auto", "openai"),
        ("anthropic/claude-sonnet-5", "auto", "openai"),  # relays like OpenRouter
        ("claude-haiku-4-5-20251001", "openai", "openai"),
        ("gpt-5-nano", "anthropic", "anthropic"),
    ],
)
def test_detect_format(model: str, setting: str, expected: str) -> None:
    assert detect_format(model, setting) == expected


def test_anthropic_request_shape_and_reply() -> None:
    server = Server([json_response(ANTHROPIC_REPLY)])
    reply = run(
        make(server).complete(
            model="claude-haiku-4-5-20251001",
            system="Be brief.",
            user="Say OK",
            max_tokens=50,
            temperature=0.2,
        )
    )
    request, body = sent(server)
    assert request.method == "POST"
    assert str(request.url) == "https://relay.example.com/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    assert body == {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 50,
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "Say OK"}],
        "temperature": 0.2,
    }
    assert reply.text == "OK"
    assert reply.model == "claude-haiku-4-5-20251001"
    assert (reply.input_tokens, reply.output_tokens) == (13, 1)
    assert reply.finish_reason == "end_turn"


def test_openai_request_shape_and_reply() -> None:
    server = Server([json_response(OPENAI_REPLY)])
    reply = run(
        make(server).complete(
            model="gpt-5-nano",
            system="Be brief.",
            user="Say OK",
            max_tokens=50,
            json_mode=True,
        )
    )
    request, body = sent(server)
    assert str(request.url) == "https://relay.example.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert "x-api-key" not in request.headers
    assert body == {
        "model": "gpt-5-nano",
        "max_tokens": 50,
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Say OK"},
        ],
        "response_format": {"type": "json_object"},
    }
    assert reply.text == "OK"
    assert reply.model == "gpt-5-nano-2025-08-07"
    assert (reply.input_tokens, reply.output_tokens) == (13, 4)
    assert reply.finish_reason == "stop"


def test_optional_fields_are_left_out_when_not_given() -> None:
    server = Server([json_response(OPENAI_REPLY), json_response(ANTHROPIC_REPLY)])
    c = make(server)
    run(c.complete(model="gpt-5-nano", user="hi"))
    run(c.complete(model="claude-haiku-4-5-20251001", user="hi", json_mode=True))
    _, openai_body = sent(server, 0)
    _, anthropic_body = sent(server, 1)
    assert "temperature" not in openai_body and "response_format" not in openai_body
    assert "temperature" not in anthropic_body and "system" not in anthropic_body
    assert "response_format" not in anthropic_body  # json_mode only applies to OpenAI
    assert openai_body["messages"] == [{"role": "user", "content": "hi"}]


def test_one_client_routes_each_model_to_its_own_format() -> None:
    server = Server([json_response(ANTHROPIC_REPLY), json_response(OPENAI_REPLY)])
    c = make(server)
    run(c.complete(model="claude-sonnet-5-5", user="hi"))
    run(c.complete(model="gpt-5-nano", user="hi"))
    assert server.requests[0].url.path == "/v1/messages"
    assert server.requests[1].url.path == "/v1/chat/completions"


def test_the_format_can_be_forced() -> None:
    server = Server([json_response(OPENAI_REPLY)])
    run(
        make(server, api_format="openai").complete(model="claude-sonnet-5-5", user="hi")
    )
    assert server.requests[0].url.path == "/v1/chat/completions"


@pytest.mark.parametrize(
    "base",
    [
        "https://relay.example.com",
        "https://relay.example.com/",
        "https://relay.example.com/v1",
        "https://relay.example.com/v1/",
    ],
)
def test_the_base_url_may_or_may_not_end_in_v1(base: str) -> None:
    server = Server([json_response(OPENAI_REPLY)])
    run(make(server, base_url=base).complete(model="gpt-5-nano", user="hi"))
    assert (
        str(server.requests[0].url) == "https://relay.example.com/v1/chat/completions"
    )


def test_without_a_base_url_the_official_api_is_used() -> None:
    server = Server([json_response(ANTHROPIC_REPLY), json_response(OPENAI_REPLY)])
    c = make(server, base_url=None)
    run(c.complete(model="claude-haiku-4-5-20251001", user="hi"))
    run(c.complete(model="gpt-5-nano", user="hi"))
    assert server.requests[0].url.host == "api.anthropic.com"
    assert server.requests[1].url.host == "api.openai.com"


def test_text_blocks_are_joined_and_other_blocks_ignored() -> None:
    reply_body = {
        "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ],
        "usage": {},
    }
    server = Server([json_response(reply_body)])
    reply = run(make(server).complete(model="claude-sonnet-5-5", user="hi"))
    assert reply.text == "Hello world"
    assert reply.model == "claude-sonnet-5-5"  # falls back to the requested name
    assert reply.input_tokens is None


def test_an_empty_openai_reply_is_returned_with_its_finish_reason() -> None:
    body = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
    reply = run(
        make(Server([json_response(body)])).complete(model="gpt-5-nano", user="hi")
    )
    assert reply.text == ""
    assert reply.finish_reason == "length"


def test_newer_openai_models_get_max_completion_tokens_after_a_rejection() -> None:
    rejection = json_response(
        {"error": {"message": "Use 'max_completion_tokens' instead of 'max_tokens'."}},
        400,
    )
    server = Server([rejection, json_response(OPENAI_REPLY)])
    reply = run(make(server).complete(model="gpt-5-nano", user="hi", max_tokens=77))
    _, first = sent(server, 0)
    _, second = sent(server, 1)
    assert first["max_tokens"] == 77
    assert "max_tokens" not in second
    assert second["max_completion_tokens"] == 77
    assert reply.text == "OK"


# ------------------------------------------------------------------------- errors


def test_no_key_is_reported_without_a_request() -> None:
    server = Server()
    with pytest.raises(LLMError) as info:
        run(make(server, api_key="").complete(model="gpt-5-nano", user="hi"))
    assert info.value.kind == "auth"
    assert server.requests == []


@pytest.mark.parametrize(
    ("status", "kind"), [(401, "auth"), (403, "auth"), (404, "model"), (400, "model")]
)
def test_failures_are_classified_and_explained(status: int, kind: str) -> None:
    server = Server([json_response({"error": {"message": "nope, bad things"}}, status)])
    with pytest.raises(LLMError) as info:
        run(make(server).complete(model="gpt-5-nano", user="hi"))
    assert info.value.kind == kind
    assert "nope, bad things" in info.value.message  # the endpoint's own explanation
    assert len(server.requests) == 1  # not retried


def test_rate_limits_are_retried_then_reported() -> None:
    server = Server([json_response({}, 429)] * 4)
    with pytest.raises(LLMError) as info:
        run(make(server).complete(model="gpt-5-nano", user="hi"))
    assert info.value.kind == "rate_limited"
    assert len(server.requests) == 4


def test_a_transient_failure_is_retried_to_success() -> None:
    server = Server([json_response({}, 503), json_response(OPENAI_REPLY)])
    assert run(make(server).complete(model="gpt-5-nano", user="hi")).text == "OK"


def test_after_repeated_failures_the_client_stops_asking() -> None:
    server = Server([json_response({}, 500)] * 12)
    c = make(server)
    kinds = []
    for _ in range(4):
        with pytest.raises(LLMError) as info:
            run(c.complete(model="gpt-5-nano", user="hi"))
        kinds.append(info.value.kind)
    assert kinds == ["error", "error", "error", "skipped"]


@pytest.mark.parametrize("payload", ["not json at all", "[1, 2]", '{"choices": "x"}'])
def test_unreadable_answers_are_errors(payload: str) -> None:
    response = httpx.Response(200, text=payload)
    with pytest.raises(LLMError) as info:
        run(make(Server([response])).complete(model="gpt-5-nano", user="hi"))
    assert info.value.kind == "error"


def test_the_key_never_appears_in_errors() -> None:
    echoing = httpx.Response(
        401, json={"error": {"message": f"Invalid key {KEY} for this relay"}}
    )
    with pytest.raises(LLMError) as info:
        run(make(Server([echoing])).complete(model="gpt-5-nano", user="hi"))
    assert KEY not in info.value.message
    assert "***" in info.value.message


# -------------------------------------------------------------------- model list


def test_list_models_returns_sorted_ids_and_sends_both_auth_styles() -> None:
    body = {"data": [{"id": "gpt-5-nano"}, {"id": "claude-sonnet-5-5"}, {"nope": 1}]}
    server = Server([json_response(body)])
    models = run(make(server).list_models())
    assert models == ["claude-sonnet-5-5", "gpt-5-nano"]
    request = server.requests[0]
    assert request.url.path == "/v1/models"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["x-api-key"] == KEY


def test_list_models_is_empty_when_the_endpoint_has_no_list() -> None:
    assert run(make(Server([json_response({}, 404)])).list_models()) == []
    assert run(make(Server([httpx.Response(200, text="<html>")])).list_models()) == []
