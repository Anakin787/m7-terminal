"""HTTP client for the Toss Securities Open API.

Responsibilities kept in one place so the endpoint wrappers stay thin:

* OAuth token issuance, caching and refresh
* per-group client-side rate limiting
* retry with backoff for 429/5xx
* unwrapping the common ``{"result": ...}`` envelope
* turning error envelopes into typed exceptions
* refusing writes unless the caller explicitly asked for them
"""

import json
import os
import random
import threading
import time

import requests

from src.toss._filelock import FileLock
from src.toss.errors import (
    TossApiError,
    TossAuthError,
    TossForbiddenError,
    TossRateLimitError,
    TossServerError,
    TossWriteBlockedError,
    error_from_response,
)
from src.toss.ratelimit import RateLimiter

DEFAULT_BASE_URL = "https://openapi.tossinvest.com"
TOKEN_PATH = "/oauth2/token"

#: Refresh this many seconds before the token actually expires, so a request
#: that starts just under the wire does not fail mid-flight.
EXPIRY_MARGIN_SECONDS = 60

#: Retry budget for transient failures (429 and 5xx).
MAX_RETRIES = 3

#: Only these methods are permitted when allow_write is False.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _mask(value):
    if not value:
        return ""
    text = str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


class TossClient:
    """A synchronous, thread-safe client for one Toss API client credential.

    ``allow_write`` defaults to False: Phase 1 is a reporting tool, and an
    order endpoint reached by accident is not a recoverable mistake. The
    trading module of Phase 2 must construct its own client with
    ``allow_write=True``.
    """

    def __init__(
        self,
        client_id,
        client_secret,
        *,
        base_url=DEFAULT_BASE_URL,
        token_cache=".toss_token.json",
        allow_write=False,
        timeout=10,
        session=None,
        rate_limiter=None,
        sleep=time.sleep,
        clock=time.time,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.token_cache = token_cache
        self.allow_write = allow_write
        self.timeout = timeout
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        self._clock = clock

        self._token = None
        self._expires_at = 0.0
        self._token_lock = threading.Lock()
        #: Counts token issuances. Tests and the smoke script assert that a
        #: warm cache does not increment this.
        self.token_issue_count = 0

    def __repr__(self):
        return (
            f"TossClient(client_id={_mask(self.client_id)!r}, "
            f"base_url={self.base_url!r}, allow_write={self.allow_write})"
        )

    # ---------------------------------------------------------------- tokens

    def _read_cache(self):
        try:
            with open(self.token_cache, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if data.get("client_id") != self.client_id:
            # A different credential owns this cache entry.
            return None
        token = data.get("access_token")
        expires_at = data.get("expires_at")
        if not token or not isinstance(expires_at, (int, float)):
            return None
        return token, float(expires_at)

    def _write_cache(self, token, expires_at):
        payload = {
            "client_id": self.client_id,
            "access_token": token,
            "expires_at": expires_at,
        }
        tmp = f"{self.token_cache}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, self.token_cache)
        try:
            os.chmod(self.token_cache, 0o600)
        except OSError:
            pass  # best effort; Windows ACLs do not map cleanly

    def _issue_token(self):
        """Request a fresh token. Invalidates any token issued earlier."""
        attempt = 0
        while True:
            self.rate_limiter.acquire("AUTH")
            try:
                response = self.session.post(
                    f"{self.base_url}{TOKEN_PATH}",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    raise TossServerError(0, "network-error", str(exc)) from exc
                attempt += 1
                self._sleep(_backoff(attempt))
                continue
            break

        payload = _safe_json(response)
        if response.status_code >= 400:
            raise error_from_response(response.status_code, payload, response.headers)

        token = payload.get("access_token")
        if not token:
            raise TossApiError(
                response.status_code,
                "invalid-token-response",
                "토큰 응답에 access_token이 없습니다.",
            )
        expires_in = float(payload.get("expires_in") or 0)
        expires_at = self._clock() + max(0.0, expires_in - EXPIRY_MARGIN_SECONDS)
        self.token_issue_count += 1
        return token, expires_at

    def _access_token(self, force_refresh=False):
        """Return a valid token, reusing the on-disk cache when possible.

        Toss keeps exactly one valid token per client and invalidates the
        previous one on every issuance, so this path is guarded by both an
        in-process lock and a cross-process file lock.
        """
        with self._token_lock:
            now = self._clock()
            if not force_refresh and self._token and now < self._expires_at:
                return self._token

            with FileLock(self.token_cache):
                if not force_refresh:
                    cached = self._read_cache()
                    if cached and self._clock() < cached[1]:
                        self._token, self._expires_at = cached
                        return self._token

                token, expires_at = self._issue_token()
                self._write_cache(token, expires_at)
                self._token, self._expires_at = token, expires_at
                return token

    # --------------------------------------------------------------- request

    def request(
        self,
        method,
        path,
        *,
        group=None,
        params=None,
        json_body=None,
        account_seq=None,
        headers=None,
    ):
        """Issue a request and return the unwrapped ``result`` payload."""
        method = method.upper()
        if method not in _READ_METHODS and not self.allow_write:
            raise TossWriteBlockedError(
                f"{method} {path} 는 읽기 전용 클라이언트에서 호출할 수 없습니다. "
                "주문 계열 호출은 allow_write=True 로 생성한 클라이언트가 필요합니다."
            )

        url = f"{self.base_url}{path}"
        attempt = 0
        refreshed = False

        while True:
            request_headers = {"Accept": "application/json"}
            if headers:
                request_headers.update(headers)
            if account_seq is not None:
                request_headers["X-Tossinvest-Account"] = str(account_seq)
            request_headers["Authorization"] = f"Bearer {self._access_token()}"

            self.rate_limiter.acquire(group)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=request_headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    raise TossServerError(0, "network-error", str(exc)) from exc
                attempt += 1
                self._sleep(_backoff(attempt))
                continue

            self.rate_limiter.observe(group, response.headers)

            if response.status_code < 400:
                return _unwrap(_safe_json(response))

            error = error_from_response(
                response.status_code, _safe_json(response), response.headers
            )

            # An expired token is worth exactly one forced refresh. Anything
            # more would mean our credential is simply wrong.
            if isinstance(error, TossAuthError) and not refreshed:
                refreshed = True
                self._access_token(force_refresh=True)
                continue

            if isinstance(error, TossForbiddenError):
                raise TossForbiddenError(
                    error.status,
                    error.code,
                    f"{error.message} (허용 IP 미등록일 가능성이 높습니다. "
                    "토스 WTS > 설정 > Open API > 허용 IP 관리를 확인하세요.)",
                    request_id=error.request_id,
                    data=error.data,
                )

            if isinstance(error, TossRateLimitError) and attempt < MAX_RETRIES:
                attempt += 1
                wait = error.retry_after if error.retry_after else _backoff(attempt)
                self._sleep(wait)
                continue

            if isinstance(error, TossServerError) and attempt < MAX_RETRIES:
                attempt += 1
                self._sleep(_backoff(attempt))
                continue

            raise error

    def get(self, path, *, group=None, params=None, account_seq=None):
        return self.request(
            "GET", path, group=group, params=params, account_seq=account_seq
        )

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _backoff(attempt):
    """1s, 2s, 4s with jitter, as the Toss docs recommend."""
    return (2 ** (attempt - 1)) + random.uniform(0, 0.5)


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return {}


def _unwrap(payload):
    """Return the ``result`` field of the common BFF envelope."""
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload
