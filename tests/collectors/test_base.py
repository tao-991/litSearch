from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from litsearch.collectors.base import (
    CollectorError,
    HttpCollector,
    SourceStatus,
    build_user_agent,
)
from tests.collectors.helpers import FakeTime, Server, json_response

URL = "https://example.test/api"


class Dummy(HttpCollector):
    name = "dummy"


def make(
    server: Server, ft: FakeTime, *, min_interval: float = 0.0, **kwargs: object
) -> Dummy:
    return Dummy(
        server.client(),
        min_interval=min_interval,
        sleep=ft.sleep,
        clock=ft.clock,
        **kwargs,  # type: ignore[arg-type]
    )


def run(coro):
    return asyncio.run(coro)


def test_success_returns_the_response() -> None:
    server = Server([json_response({"ok": True})])
    c = make(server, FakeTime())
    resp = run(c._get(URL))
    assert resp.json() == {"ok": True}
    assert c.n_requests == 1


def test_rate_limit_is_retried_with_growing_backoff() -> None:
    server = Server([json_response({}, 429)] * 3 + [json_response({"ok": 1})])
    ft = FakeTime()
    c = make(server, ft)
    assert run(c._get(URL)).json() == {"ok": 1}
    assert ft.sleeps == [2.0, 4.0, 8.0]
    assert c.n_requests == 4


def test_persistent_rate_limit_raises_rate_limited() -> None:
    server = Server([json_response({}, 429)] * 4)
    c = make(server, FakeTime())
    with pytest.raises(CollectorError) as info:
        run(c._get(URL))
    assert info.value.status is SourceStatus.RATE_LIMITED
    assert c.n_requests == 4  # first try + 3 retries


def test_retry_after_is_honoured_and_capped() -> None:
    server = Server(
        [
            json_response({}, 429, **{"retry-after": "5"}),
            json_response({}, 429, **{"retry-after": "600"}),
            json_response({"ok": 1}),
        ]
    )
    ft = FakeTime()
    c = make(server, ft)
    run(c._get(URL))
    assert ft.sleeps == [5.0, 60.0]


def test_server_errors_are_retried_then_reported_as_error() -> None:
    server = Server([json_response({}, 503)] * 4)
    c = make(server, FakeTime())
    with pytest.raises(CollectorError) as info:
        run(c._get(URL))
    assert info.value.status is SourceStatus.ERROR


def test_network_error_is_retried() -> None:
    server = Server([httpx.ConnectError("boom"), json_response({"ok": 1})])
    ft = FakeTime()
    c = make(server, ft)
    assert run(c._get(URL)).json() == {"ok": 1}
    assert ft.sleeps == [2.0]


def test_forbidden_is_blocked_and_not_retried() -> None:
    server = Server([json_response({}, 403)])
    c = make(server, FakeTime())
    with pytest.raises(CollectorError) as info:
        run(c._get(URL))
    assert info.value.status is SourceStatus.BLOCKED
    assert c.n_requests == 1


def test_bad_request_is_error_and_not_retried() -> None:
    server = Server([json_response({"error": "bad"}, 400)])
    c = make(server, FakeTime())
    with pytest.raises(CollectorError) as info:
        run(c._get(URL))
    assert info.value.status is SourceStatus.ERROR
    assert c.n_requests == 1


def test_expected_not_found_is_returned_to_the_caller() -> None:
    server = Server([json_response({}, 404)])
    c = make(server, FakeTime())
    resp = run(c._get(URL, ok_statuses=(200, 404)))
    assert resp.status_code == 404


def test_breaker_opens_after_repeated_failures_and_skips_without_requests() -> None:
    server = Server([json_response({}, 500)] * 3)
    c = make(server, FakeTime(), max_retries=0, breaker_limit=3)
    for _ in range(3):
        with pytest.raises(CollectorError):
            run(c._get(URL))
    assert c.breaker_open
    with pytest.raises(CollectorError) as info:
        run(c._get(URL))
    assert info.value.status is SourceStatus.SKIPPED
    assert c.n_requests == 3  # the fourth call never reached the network


def test_a_success_resets_the_failure_count() -> None:
    fail = json_response({}, 500)
    server = Server([fail, fail, json_response({}), fail, fail])
    c = make(server, FakeTime(), max_retries=0, breaker_limit=3)
    outcomes = []
    for _ in range(5):
        try:
            run(c._get(URL))
            outcomes.append("ok")
        except CollectorError:
            outcomes.append("fail")
    assert outcomes == ["fail", "fail", "ok", "fail", "fail"]
    assert not c.breaker_open


def test_minimum_interval_between_requests_is_enforced() -> None:
    server = Server([json_response({})] * 3)
    ft = FakeTime()
    c = make(server, ft, min_interval=3.0)

    async def scenario() -> None:
        await c._get(URL)  # first request: no waiting
        await c._get(URL)  # immediately again: must wait 3 s
        ft.now += 10  # a long pause passes by itself
        await c._get(URL)  # no waiting needed

    run(scenario())
    assert ft.sleeps == [3.0]


def test_requests_to_one_source_never_overlap() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ft = FakeTime()
    c = Dummy(client, min_interval=0.0, sleep=ft.sleep, clock=ft.clock)

    async def scenario() -> None:
        await asyncio.gather(*(c._get(URL) for _ in range(5)))

    run(scenario())
    assert peak == 1


def test_secrets_are_sent_but_never_leak_into_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SECRET-KEY-123"
    sent = Server([json_response({})])
    c = make(sent, FakeTime())
    run(c._get(URL, params={"q": "x"}, secret_params={"api_key": secret}))
    assert sent.requests[0].url.params["api_key"] == secret
    assert sent.requests[0].url.params["q"] == "x"

    failing = Server([httpx.ConnectError(f"failed for {URL}?api_key={secret}")] * 4)
    c2 = make(failing, FakeTime())
    with caplog.at_level(logging.DEBUG), pytest.raises(CollectorError) as info:
        run(c2._get(URL, secret_params={"api_key": secret}))
    assert secret not in info.value.message
    assert secret not in caplog.text
    assert secret not in str(c2.last_error)


def test_build_user_agent() -> None:
    assert build_user_agent("0.1.0", None) == "litsearch/0.1.0"
    assert build_user_agent("0.1.0", "me@example.org") == (
        "litsearch/0.1.0 (mailto:me@example.org)"
    )
