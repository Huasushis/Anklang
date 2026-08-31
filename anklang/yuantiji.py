"""Bounded client for the public yuantiji.ac similarity search API.

Only a problem statement and fixed search options are sent.  Responses are projected into
Anklang's candidate contract; upstream bodies and exception text never cross the HTTP
boundary or enter logs.
"""
from __future__ import annotations

import http.client
import json
import math
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .backends import CompletionReason


_MAX_QUERY_CHARACTERS = 16_000
_MAX_RESPONSE_BYTES = 4_000_000
_MAX_STATEMENT_UTF16 = 32_000
_MAX_TEXT_UTF16 = 200
_USER_AGENT = "Anklang/0.2 (+https://github.com/Huasushis/Anklang)"


class YuantijiError(RuntimeError):
    """A fixed, safe upstream failure classification."""

    def __init__(
        self,
        *,
        reason_code: CompletionReason,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__("yuantiji search unavailable")


@dataclass(frozen=True)
class YuantijiSearchResult:
    candidates: list[dict[str, Any]]
    partial: bool = False


class YuantijiClient:
    """Small, polite client for ``POST /api/search`` and ``GET /api/health``.

    Calls are serialized per process and separated by a minimum interval so one Urmotiv
    deployment cannot accidentally burst the public service.  A transient failure is
    retried at most once.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 60.0,
        minimum_interval_seconds: float = 1.0,
        max_retries: int = 1,
        opener: Any | None = None,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ) -> None:
        self._search_url = f"{base_url.rstrip('/')}/api/search"
        self._health_url = f"{base_url.rstrip('/')}/api/health"
        self._timeout_seconds = max(1.0, min(float(timeout_seconds), 300.0))
        self._minimum_interval_seconds = max(
            0.0, min(float(minimum_interval_seconds), 60.0)
        )
        self._max_retries = max(0, min(int(max_retries), 1))
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._sleeper = sleeper
        self._request_lock = threading.Lock()
        self._last_request_at: float | None = None
        self._health_cache: tuple[float, dict[str, Any]] | None = None

    def health(self) -> dict[str, Any]:
        now = self._clock()
        cached = self._health_cache
        if cached is not None and cached[0] > now:
            return dict(cached[1])
        try:
            payload = self._request_json(self._health_url, None)
        except YuantijiError:
            payload = {"ok": False}
        safe: dict[str, Any] = {"ok": payload.get("ok") is True}
        problems = payload.get("problems")
        if isinstance(problems, int) and not isinstance(problems, bool) and problems >= 0:
            safe["problems"] = problems
        self._health_cache = (self._clock() + 60.0, safe)
        return dict(safe)

    def cached_health(self) -> dict[str, Any] | None:
        """Return a recent health result without starting a network request."""

        cached = self._health_cache
        if cached is None or cached[0] <= self._clock():
            return None
        return dict(cached[1])

    def search(self, query: str, k: int, *, rerank: bool = False) -> YuantijiSearchResult:
        body = json.dumps(
            {
                "query": query[:_MAX_QUERY_CHARACTERS],
                "k": max(1, min(int(k), 50)),
                "rewrite": False,
                "skip_short": True,
                "sources": [],
                "rerank": bool(rerank),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        payload = self._request_json(self._search_url, body)
        results = payload.get("results")
        if not isinstance(results, list):
            raise YuantijiError(
                reason_code="search_backend_invalid", retryable=False
            )
        candidates: list[dict[str, Any]] = []
        invalid = 0
        for raw in results:
            candidate = self._map_candidate(raw)
            if candidate is None:
                invalid += 1
            else:
                candidates.append(candidate)
        if results and not candidates:
            raise YuantijiError(
                reason_code="search_backend_invalid", retryable=False
            )
        return YuantijiSearchResult(candidates, partial=invalid > 0)

    def _request_json(self, url: str, body: bytes | None) -> dict[str, Any]:
        with self._request_lock:
            last_error: YuantijiError | None = None
            for attempt in range(self._max_retries + 1):
                self._wait_for_interval()
                request = urllib.request.Request(
                    url,
                    data=body,
                    method="POST" if body is not None else "GET",
                    headers={
                        "Accept": "application/json",
                        "User-Agent": _USER_AGENT,
                        **(
                            {"Content-Type": "application/json"}
                            if body is not None
                            else {}
                        ),
                    },
                )
                try:
                    payload = self._perform_once(request)
                except YuantijiError as error:
                    self._last_request_at = self._clock()
                    last_error = error
                    if error.retryable and attempt < self._max_retries:
                        delay = float(error.retry_after_seconds or (attempt + 1))
                        self._sleeper(min(delay, 30.0))
                        continue
                    raise
                self._last_request_at = self._clock()
                return payload
            assert last_error is not None
            raise last_error

    def _wait_for_interval(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self._minimum_interval_seconds - (
            self._clock() - self._last_request_at
        )
        if remaining > 0:
            self._sleeper(remaining)

    def _perform_once(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise YuantijiError(
                    reason_code="search_rate_limited",
                    retryable=True,
                    retry_after_seconds=_retry_after(error.headers),
                ) from error
            if error.code >= 500:
                raise YuantijiError(
                    reason_code="search_backend_unavailable", retryable=True
                ) from error
            raise YuantijiError(
                reason_code="search_backend_unavailable", retryable=False
            ) from error
        except TimeoutError as error:
            raise YuantijiError(
                reason_code="search_timeout", retryable=True
            ) from error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise YuantijiError(
                reason_code="search_backend_unavailable", retryable=True
            ) from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise YuantijiError(
                reason_code="search_backend_invalid", retryable=False
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise YuantijiError(
                reason_code="search_backend_invalid", retryable=False
            ) from error
        if not isinstance(payload, dict):
            raise YuantijiError(
                reason_code="search_backend_invalid", retryable=False
            )
        return payload

    @staticmethod
    def _map_candidate(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        external_id = _text(raw.get("uid"), _MAX_TEXT_UTF16)
        title = _text(raw.get("title"), _MAX_TEXT_UTF16)
        source = _text(raw.get("src"), 80)
        similarity = _score(raw.get("rr"))
        if similarity is None:
            similarity = _score(raw.get("cos"))
        if None in (external_id, title, source, similarity):
            return None
        candidate: dict[str, Any] = {
            "source": source,
            "externalId": external_id,
            "title": title,
            "similarity": max(0.0, min(1.0, float(similarity))),
            "metadata": {"search_provider": "yuantiji"},
        }
        url = raw.get("url")
        if isinstance(url, str) and url.strip():
            candidate["url"] = url.strip()
        original = raw.get("original")
        if isinstance(original, str) and original.strip():
            statement, truncated = _truncate_utf16(
                original.strip(), _MAX_STATEMENT_UTF16
            )
            candidate["statement"] = statement
            if truncated:
                candidate["statementTruncated"] = True
        return candidate


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or _utf16_length(normalized) > limit:
        return None
    return normalized


def _utf16_length(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _truncate_utf16(value: str, limit: int) -> tuple[str, bool]:
    if _utf16_length(value) <= limit:
        return value, False
    current = 0
    kept: list[str] = []
    for character in value:
        width = 2 if ord(character) > 0xFFFF else 1
        if current + width > limit:
            break
        kept.append(character)
        current += width
    return "".join(kept), True


def _retry_after(headers: Any) -> int | None:
    try:
        raw = headers.get("Retry-After")
        value = int(raw)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if 1 <= value <= 86_400 else None
