"""TossClient behaviour under a fake transport - no credentials needed."""

import json
import os
import tempfile

import pytest
import requests

from src.toss.client import TossClient
from src.toss.errors import (
    TossAuthError,
    TossForbiddenError,
    TossNotFoundError,
    TossServerError,
    TossWriteBlockedError,
)
from src.toss.ratelimit import RateLimiter


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """Records calls and replays queued responses."""

    def __init__(self):
        self.token_responses = []
        self.token_exceptions = []
        self.responses = []
        self.token_calls = 0
        self.requests = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.token_calls += 1
        if self.token_exceptions:
            raise self.token_exceptions.pop(0)
        if self.token_responses:
            return self.token_responses.pop(0)
        return FakeResponse(
            200, {"access_token": f"tok{self.token_calls}", "token_type": "Bearer", "expires_in": 3600}
        )

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.requests.append(
            {"method": method, "url": url, "params": params, "headers": headers}
        )
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"result": {"ok": True}})

    def close(self):
        pass


@pytest.fixture
def cache_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.join(tmp, "token.json")


def make_client(session, cache_path, **kwargs):
    sleeps = []
    client = TossClient(
        "cid",
        "secret",
        token_cache=cache_path,
        session=session,
        rate_limiter=RateLimiter(limits={}, sleep=lambda _: None),
        sleep=sleeps.append,
        **kwargs,
    )
    client.recorded_sleeps = sleeps
    return client


def test_unwraps_result_envelope(cache_path):
    session = FakeSession()
    session.responses = [FakeResponse(200, {"result": [{"symbol": "005930"}]})]
    client = make_client(session, cache_path)

    assert client.get("/api/v1/prices", group="MARKET_DATA") == [{"symbol": "005930"}]


def test_token_is_issued_once_and_reused(cache_path):
    session = FakeSession()
    client = make_client(session, cache_path)

    client.get("/api/v1/accounts")
    client.get("/api/v1/holdings")

    assert session.token_calls == 1
    assert client.token_issue_count == 1


def test_token_cache_is_shared_across_client_instances(cache_path):
    """A second process must not force a refresh - that would invalidate the
    token the first one is still using."""
    first = make_client(FakeSession(), cache_path)
    first.get("/api/v1/accounts")

    second_session = FakeSession()
    second = make_client(second_session, cache_path)
    second.get("/api/v1/accounts")

    assert second_session.token_calls == 0
    assert second.token_issue_count == 0

    with open(cache_path, encoding="utf-8") as handle:
        assert json.load(handle)["client_id"] == "cid"


def test_expired_token_triggers_exactly_one_refresh(cache_path):
    session = FakeSession()
    session.responses = [
        FakeResponse(401, {"error": {"code": "expired-token", "message": "expired"}}),
        FakeResponse(200, {"result": {"ok": True}}),
    ]
    client = make_client(session, cache_path)

    assert client.get("/api/v1/holdings") == {"ok": True}
    assert session.token_calls == 2


def test_repeated_401_is_raised_not_looped(cache_path):
    session = FakeSession()
    session.responses = [
        FakeResponse(401, {"error": {"code": "invalid-token", "message": "bad"}}),
        FakeResponse(401, {"error": {"code": "invalid-token", "message": "bad"}}),
    ]
    client = make_client(session, cache_path)

    with pytest.raises(TossAuthError):
        client.get("/api/v1/holdings")


def test_oauth_style_token_error_does_not_crash(cache_path):
    """The token endpoint uses the standard flat OAuth envelope
    (``{"error": "invalid_client", "error_description": ...}``), not Toss's
    usual nested one - it must not be mistaken for a dict-shaped envelope."""
    session = FakeSession()
    session.token_responses = [
        FakeResponse(
            401,
            {"error": "invalid_client", "error_description": "Client authentication failed: client_secret"},
        )
    ]
    client = make_client(session, cache_path)

    with pytest.raises(TossAuthError) as excinfo:
        client.get("/api/v1/accounts")
    assert excinfo.value.code == "invalid_client"
    assert "client_secret" in excinfo.value.message


def test_token_issuance_retries_after_network_error(cache_path):
    session = FakeSession()
    session.token_exceptions = [requests.exceptions.ReadTimeout("timed out")]
    client = make_client(session, cache_path)

    assert client.get("/api/v1/accounts") == {"ok": True}
    assert session.token_calls == 2
    assert client.token_issue_count == 1
    assert client.recorded_sleeps  # backoff was applied before the retry


def test_token_issuance_gives_up_after_exhausting_retries(cache_path):
    session = FakeSession()
    session.token_exceptions = [
        requests.exceptions.ReadTimeout("timed out"),
        requests.exceptions.ReadTimeout("timed out"),
        requests.exceptions.ReadTimeout("timed out"),
        requests.exceptions.ReadTimeout("timed out"),
    ]
    client = make_client(session, cache_path)

    with pytest.raises(TossServerError) as excinfo:
        client.get("/api/v1/accounts")
    assert excinfo.value.code == "network-error"
    assert session.token_calls == 4  # 1 initial attempt + 3 retries
    assert client.token_issue_count == 0
    assert len(client.recorded_sleeps) == 3


def test_429_waits_for_retry_after(cache_path):
    session = FakeSession()
    session.responses = [
        FakeResponse(
            429,
            {"error": {"code": "rate-limit-exceeded", "message": "slow down"}},
            headers={"Retry-After": "2"},
        ),
        FakeResponse(200, {"result": {"ok": True}}),
    ]
    client = make_client(session, cache_path)

    assert client.get("/api/v1/prices", group="MARKET_DATA") == {"ok": True}
    assert client.recorded_sleeps == [2.0]


def test_403_message_points_at_the_ip_allowlist(cache_path):
    session = FakeSession()
    session.responses = [
        FakeResponse(403, {"error": {"code": "edge-blocked", "message": "blocked"}})
    ]
    client = make_client(session, cache_path)

    with pytest.raises(TossForbiddenError) as excinfo:
        client.get("/api/v1/accounts")
    assert "허용 IP" in str(excinfo.value)


def test_404_maps_to_not_found(cache_path):
    session = FakeSession()
    session.responses = [
        FakeResponse(404, {"error": {"code": "stock-not-found", "message": "nope"}})
    ]
    client = make_client(session, cache_path)

    with pytest.raises(TossNotFoundError) as excinfo:
        client.get("/api/v1/stocks/XXXX/warnings")
    assert excinfo.value.code == "stock-not-found"


def test_write_is_blocked_by_default(cache_path):
    client = make_client(FakeSession(), cache_path)

    with pytest.raises(TossWriteBlockedError):
        client.request("POST", "/api/v1/orders", json_body={"symbol": "005930"})


def test_write_is_allowed_when_explicitly_enabled(cache_path):
    session = FakeSession()
    session.responses = [FakeResponse(200, {"result": {"orderId": "1"}})]
    client = make_client(session, cache_path, allow_write=True)

    assert client.request("POST", "/api/v1/orders", json_body={}) == {"orderId": "1"}


def test_account_header_is_sent(cache_path):
    session = FakeSession()
    client = make_client(session, cache_path)

    client.get("/api/v1/holdings", account_seq=7)

    assert session.requests[0]["headers"]["X-Tossinvest-Account"] == "7"


def test_repr_masks_the_secret(cache_path):
    client = make_client(FakeSession(), cache_path)
    assert "secret" not in repr(client)
