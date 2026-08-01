"""yuantiji.ac 反向代理客户端。

把 Urmotiv 的查重请求翻译成 yuantiji.ac 的公开检索请求，再把结果映射回契约里的
candidate 结构。对上游保持克制：串行调用、请求之间留最小间隔、失败后只重试有限
次数；连续失败时暂停调用一段时间，避免给个人自费维护的服务继续施压。

/api/search 的字段来自对 yuantiji.ac 前端内联 JS 的逆向，并已用一次真实调用确认：
  请求  {query, k, rewrite, skip_short, sources, rerank}
  响应  {results: [{uid, title, url, src, cos, rr, base_rank, also, original, ...}]}
其中 cos 是向量余弦相似度、rr 是重排分数（可能为 null）。相似度优先取 rr，没有再取 cos。
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

_MAX_RESPONSE_BYTES = 4_000_000
_MAX_QUERY_CHARACTERS = 16_000
_MAX_CANDIDATE_SOURCE_UTF16 = 80
_MAX_CANDIDATE_EXTERNAL_ID_UTF16 = 200
_MAX_CANDIDATE_TITLE_UTF16 = 200
_JS_TRIM_CHARACTERS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
DEFAULT_REQUEST_QUEUE_SECONDS = 5.0
_USER_AGENT = "Anklang/0.1"


class YuantijiError(RuntimeError):
    """不携带上游正文的固定分类错误。异常文本不得进入 HTTP 响应。"""

    def __init__(
        self,
        message: str,
        *,
        reason_code: CompletionReason,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        if reason_code == "complete":
            raise ValueError("上游错误不能使用 complete 原因。")
        if not isinstance(retryable, bool):
            raise ValueError("上游错误的重试状态不合法。")
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, int)
            or not 1 <= retry_after_seconds <= 86_400
            or not retryable
        ):
            raise ValueError("上游错误的重试等待时间不合法。")
        self.reason_code = reason_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class _RetryableRequestError(YuantijiError):
    """一次可能是暂时故障的上游调用失败，可以在有限次数内重试。"""


@dataclass(frozen=True)
class YuantijiSearchResult:
    candidates: list[dict[str, Any]]
    partial: bool = False
    reason_code: CompletionReason = "complete"
    retryable: bool = False
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidates, list)
            or not isinstance(self.partial, bool)
            or not isinstance(self.retryable, bool)
        ):
            raise ValueError("上游结果状态不合法。")
        if self.partial:
            if self.reason_code == "complete":
                raise ValueError("部分上游结果必须说明固定原因。")
        elif (
            self.reason_code != "complete"
            or self.retryable
            or self.retry_after_seconds is not None
        ):
            raise ValueError("完整上游结果必须使用固定完成状态。")
        retry_after = self.retry_after_seconds
        if retry_after is not None and (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, int)
            or not 1 <= retry_after <= 86_400
            or not self.retryable
        ):
            raise ValueError("上游重试等待时间不合法。")


def calculate_search_budget_seconds(
    timeout_seconds: float,
    minimum_interval_seconds: float,
    max_retries: int,
    retry_base_delay_seconds: float,
    request_queue_seconds: float = DEFAULT_REQUEST_QUEUE_SECONDS,
) -> float:
    """按配置算出一次检索最多可以等待多少秒。

    计算包含本地排队、第一次调用前可能需要补足的请求间隔、每次上游调用，以及
    再次尝试前逐步增加的等待。运行时会让这些步骤共同使用这一份时间。
    """

    timeout = max(0.0, timeout_seconds)
    interval = max(0.0, minimum_interval_seconds)
    retries = max(0, max_retries)
    retry_delay = max(0.0, retry_base_delay_seconds)
    total = max(0.0, request_queue_seconds) + interval
    total += (retries + 1) * timeout
    for attempt in range(retries):
        total += max(interval, retry_delay * (2**attempt))
    return total


class YuantijiClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        minimum_interval_seconds: float,
        opener: Any | None = None,
        *,
        max_retries: int = 1,
        retry_base_delay_seconds: float = 0.5,
        circuit_failure_threshold: int = 3,
        circuit_open_seconds: float = 60.0,
        health_cache_seconds: float = 60.0,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
        request_lock: Any | None = None,
        health_refresh_lock: Any | None = None,
        search_budget_seconds: float | None = None,
        request_queue_seconds: float = DEFAULT_REQUEST_QUEUE_SECONDS,
    ) -> None:
        self._search_url = f"{base_url}/api/search"
        self._health_url = f"{base_url}/api/health"
        self._timeout = timeout_seconds
        self._minimum_interval = minimum_interval_seconds
        self._opener = opener or urllib.request.urlopen
        self._max_retries = max(0, max_retries)
        self._retry_base_delay = max(0.0, retry_base_delay_seconds)
        self._circuit_failure_threshold = max(1, circuit_failure_threshold)
        self._circuit_open_seconds = max(1.0, circuit_open_seconds)
        self._health_cache_seconds = max(1.0, health_cache_seconds)
        self._clock = clock
        self._sleeper = sleeper
        self._request_queue_seconds = max(0.0, request_queue_seconds)
        configured_budget = calculate_search_budget_seconds(
            timeout_seconds=self._timeout,
            minimum_interval_seconds=self._minimum_interval,
            max_retries=self._max_retries,
            retry_base_delay_seconds=self._retry_base_delay,
            request_queue_seconds=self._request_queue_seconds,
        )
        if search_budget_seconds is None:
            self._search_budget_seconds = configured_budget
        else:
            # 注入值只允许缩短时间，不能绕过按配置算出的上限。
            self._search_budget_seconds = min(
                configured_budget, max(0.0, search_budget_seconds)
            )
        self._request_lock = (
            request_lock if request_lock is not None else threading.Lock()
        )
        self._health_refresh_lock = (
            health_refresh_lock
            if health_refresh_lock is not None
            else threading.Lock()
        )
        self._state_lock = threading.Lock()
        self._last_call_monotonic: float | None = None
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._health_cache: tuple[float, dict[str, Any]] | None = None

    def health(self) -> dict[str, Any]:
        cached = self._read_health_cache()
        if cached is not None:
            return cached
        deadline = self._clock() + self._search_budget_seconds
        # 多个监控请求同时到达时只让一个刷新上游，其余请求在拿到锁后复用结果。
        remaining = self._remaining_time(deadline)
        if remaining <= 0 or not self._health_refresh_lock.acquire(
            timeout=remaining
        ):
            cached = self._read_health_cache()
            return cached if cached is not None else {"ok": False}
        try:
            cached = self._read_health_cache()
            if cached is not None:
                return cached
            if not self._acquire_request_slot(deadline):
                payload = {"ok": False}
            else:
                try:
                    self._ensure_circuit_closed()
                    payload = self._get(self._health_url, deadline)
                except YuantijiError:
                    payload = {"ok": False}
                finally:
                    self._request_lock.release()
            with self._state_lock:
                now = self._clock()
                expires_at = now + self._health_cache_seconds
                if self._circuit_open_until > now:
                    expires_at = min(expires_at, self._circuit_open_until)
                self._health_cache = (
                    expires_at,
                    dict(payload),
                )
            return dict(payload)
        finally:
            self._health_refresh_lock.release()

    def search(self, query: str, k: int, rerank: bool) -> YuantijiSearchResult:
        deadline = self._clock() + self._search_budget_seconds
        body = json.dumps(
            {
                # 上游明确拒绝超过 16000 个字符的查询。Python 的字符串切片按完整
                # Unicode 字符切，不会把一个多字节字符截成损坏的 UTF-8。
                "query": query[:_MAX_QUERY_CHARACTERS],
                "k": k,
                "rewrite": False,
                "skip_short": False,
                "sources": [],
                "rerank": rerank,
            }
        ).encode("utf-8")
        if not self._acquire_request_slot(deadline):
            raise YuantijiError(
                "yuantiji 当前请求较多，请稍后重试。",
                reason_code="search_timeout",
                retryable=True,
            )
        try:
            self._ensure_time_remaining(deadline)
            self._ensure_circuit_closed()
            try:
                payload = self._post(self._search_url, body, deadline)
                results = payload.get("results")
                if not isinstance(results, list):
                    raise YuantijiError(
                        "yuantiji 返回内容缺少 results 列表。",
                        reason_code="search_backend_invalid",
                        retryable=False,
                    )
                candidates: list[dict[str, Any]] = []
                invalid_candidates = 0
                for item in results:
                    if not isinstance(item, dict):
                        invalid_candidates += 1
                        continue
                    candidate = self._map_candidate(item)
                    if candidate is not None:
                        candidates.append(candidate)
                    else:
                        invalid_candidates += 1
                if results and not candidates:
                    raise YuantijiError(
                        "yuantiji 返回的候选内容不符合约定。",
                        reason_code="search_backend_invalid",
                        retryable=False,
                    )
                self._ensure_time_remaining(deadline)
            except YuantijiError:
                self._record_search_failure()
                raise
            if invalid_candidates:
                # 混合响应仍保留可信候选，但结构漂移应计入熔断，不能把它登记成
                # 一次完全健康的上游调用。
                self._record_search_failure()
                return YuantijiSearchResult(
                    candidates=candidates,
                    partial=True,
                    reason_code="search_backend_invalid",
                    retryable=False,
                )
            self._record_search_success()
            return YuantijiSearchResult(candidates=candidates)
        finally:
            self._request_lock.release()

    @staticmethod
    def _map_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
        external_id = _candidate_text(
            item.get("uid"), _MAX_CANDIDATE_EXTERNAL_ID_UTF16
        )
        title = _candidate_text(item.get("title"), _MAX_CANDIDATE_TITLE_UTF16)
        source = _candidate_text(item.get("src"), _MAX_CANDIDATE_SOURCE_UTF16)
        if external_id is None or title is None or source is None:
            return None

        rerank_score = item.get("rr")
        cosine = item.get("cos")
        similarity = _finite_score(rerank_score)
        if similarity is None:
            similarity = _finite_score(cosine)
        if similarity is None:
            return None
        similarity = max(0.0, min(1.0, similarity))

        candidate: dict[str, Any] = {
            "source": source,
            "externalId": external_id,
            "title": title,
            "similarity": similarity,
            "explanation": "该候选由公开题库的相似度信号找到，请打开来源链接人工核对。",
        }
        url = item.get("url")
        if isinstance(url, str):
            candidate["url"] = url

        original = item.get("original")
        if isinstance(original, str) and original.strip():
            snippet = " ".join(original.split())
            # 只在当前请求内供可选模型复核使用，绝不进入契约响应、缓存或日志。
            candidate["_reviewExcerpt"] = snippet[:400]
        return candidate

    def _throttle(self, deadline: float) -> None:
        self._ensure_time_remaining(deadline)
        if self._minimum_interval <= 0:
            return
        if self._last_call_monotonic is None:
            return
        elapsed = self._clock() - self._last_call_monotonic
        wait = self._minimum_interval - elapsed
        if wait > 0:
            self._sleep_with_budget(wait, deadline)

    def _acquire_request_slot(self, deadline: float) -> bool:
        # 服务按顺序访问个人维护的上游，但本地排队也必须有截止时间，避免一个
        # 慢请求让后续投稿无限等待。排队使用整次调用的同一份时间。
        remaining = self._remaining_time(deadline)
        if remaining <= 0:
            return False
        wait_seconds = min(self._request_queue_seconds, remaining)
        acquired = self._request_lock.acquire(timeout=wait_seconds)
        if not acquired:
            return False
        if self._remaining_time(deadline) <= 0:
            self._request_lock.release()
            return False
        return True

    def _get(self, url: str, deadline: float) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )
        return self._perform(request, deadline)

    def _post(self, url: str, body: bytes, deadline: float) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )
        return self._perform(request, deadline)

    def _perform(
        self, request: urllib.request.Request, deadline: float
    ) -> dict[str, Any]:
        last_error: _RetryableRequestError | None = None
        for attempt in range(self._max_retries + 1):
            self._throttle(deadline)
            try:
                parsed = self._perform_once(request, deadline)
            except _RetryableRequestError as error:
                self._last_call_monotonic = self._clock()
                last_error = error
                if attempt < self._max_retries:
                    delay = self._retry_base_delay * (2**attempt)
                    if error.retry_after_seconds is not None:
                        delay = max(delay, float(error.retry_after_seconds))
                        if delay >= self._remaining_time(deadline):
                            # 没有足够预算完整遵守上游等待时间时，不提前重试，
                            # 也不把明确的 429 错报成普通超时。
                            break
                    self._sleep_with_budget(delay, deadline)
                    continue
                break
            except YuantijiError:
                self._last_call_monotonic = self._clock()
                raise
            self._last_call_monotonic = self._clock()
            return parsed

        assert last_error is not None
        raise YuantijiError(
            "yuantiji 服务暂时不可用。",
            reason_code=last_error.reason_code,
            retryable=True,
            retry_after_seconds=last_error.retry_after_seconds,
        ) from last_error

    def _perform_once(
        self, request: urllib.request.Request, deadline: float
    ) -> dict[str, Any]:
        timeout = min(self._timeout, self._ensure_time_remaining(deadline))
        try:
            with self._opener(request, timeout=timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise _RetryableRequestError(
                    "yuantiji 暂时限制请求频率。",
                    reason_code="search_rate_limited",
                    retryable=True,
                    retry_after_seconds=_safe_retry_after_seconds(error.headers),
                ) from error
            if error.code >= 500:
                raise _RetryableRequestError(
                    "yuantiji 服务暂时不可用。",
                    reason_code="search_backend_unavailable",
                    retryable=True,
                ) from error
            raise YuantijiError(
                "yuantiji 拒绝了本次请求。",
                reason_code="search_backend_unavailable",
                retryable=False,
            ) from error
        except TimeoutError as error:
            raise _RetryableRequestError(
                "yuantiji 请求超时。",
                reason_code="search_timeout",
                retryable=True,
            ) from error
        except (
            urllib.error.URLError,
            OSError,
            http.client.HTTPException,
        ) as error:
            raise _RetryableRequestError(
                "无法连接 yuantiji 服务。",
                reason_code=(
                    "search_timeout" if _is_timeout_error(error) else "search_backend_unavailable"
                ),
                retryable=True,
            ) from error

        self._ensure_time_remaining(deadline)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise YuantijiError(
                "yuantiji 返回内容过大。",
                reason_code="search_backend_invalid",
                retryable=False,
            )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise YuantijiError(
                "yuantiji 返回内容不是有效 JSON。",
                reason_code="search_backend_invalid",
                retryable=False,
            ) from error
        if not isinstance(parsed, dict):
            raise YuantijiError(
                "yuantiji 返回内容不是 JSON 对象。",
                reason_code="search_backend_invalid",
                retryable=False,
            )
        self._ensure_time_remaining(deadline)
        return parsed

    def _remaining_time(self, deadline: float) -> float:
        return max(0.0, deadline - self._clock())

    def _ensure_time_remaining(self, deadline: float) -> float:
        remaining = self._remaining_time(deadline)
        if remaining <= 0:
            raise YuantijiError(
                "yuantiji 本次检索已达到等待上限。",
                reason_code="search_timeout",
                retryable=True,
            )
        return remaining

    def _sleep_with_budget(self, seconds: float, deadline: float) -> None:
        remaining = self._ensure_time_remaining(deadline)
        requested = max(0.0, seconds)
        sleep_seconds = min(requested, remaining)
        if sleep_seconds > 0:
            self._sleeper(sleep_seconds)
        if sleep_seconds < requested or self._remaining_time(deadline) <= 0:
            raise YuantijiError(
                "yuantiji 本次检索已达到等待上限。",
                reason_code="search_timeout",
                retryable=True,
            )

    def _ensure_circuit_closed(self) -> None:
        with self._state_lock:
            if self._circuit_open_until > self._clock():
                raise YuantijiError(
                    "yuantiji 服务连续失败，当前已暂停调用。",
                    reason_code="search_backend_unavailable",
                    retryable=True,
                )

    def _record_search_success(self) -> None:
        with self._state_lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            self._health_cache = None

    def _record_search_failure(self) -> None:
        with self._state_lock:
            self._consecutive_failures += 1
            self._health_cache = None
            if self._consecutive_failures >= self._circuit_failure_threshold:
                self._circuit_open_until = self._clock() + self._circuit_open_seconds

    def _read_health_cache(self) -> dict[str, Any] | None:
        now = self._clock()
        with self._state_lock:
            if self._circuit_open_until > now:
                return {"ok": False}
            cached = self._health_cache
            if cached is not None and cached[0] > now:
                return dict(cached[1])
        return None


def _finite_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_retry_after_seconds(headers: Any) -> int | None:
    """只接受简单整数秒，避免把上游日期或任意文本带入公开契约。"""

    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= 86_400 else None


def _is_timeout_error(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    reason = getattr(error, "reason", None)
    return isinstance(reason, TimeoutError)


def _candidate_text(value: Any, max_utf16_units: int) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip(_JS_TRIM_CHARACTERS)
    if not trimmed or _utf16_length(trimmed) > max_utf16_units:
        return None
    return trimmed


def _utf16_length(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)
