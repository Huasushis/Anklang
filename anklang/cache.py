"""按内容摘要缓存查重结果的内存实现。

只缓存已经映射成契约结构的结果，不缓存 yuantiji 的原始响应，也不落盘（题面相关内容
不应长期留存）。到期或超出容量时清理最旧的条目。
"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

_MAX_TTL_SECONDS = 7 * 24 * 60 * 60


class ResultCache:
    def __init__(
        self,
        ttl_seconds: int,
        max_entries: int,
        clock: Any = time.monotonic,
        wall_clock: Any | None = None,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= _MAX_TTL_SECONDS
        ):
            raise ValueError("缓存有效期必须在一秒到七天之间。")
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries < 1
        ):
            raise ValueError("缓存容量必须是正整数。")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, tuple[float, datetime, dict[str, Any]]]" = (
            OrderedDict()
        )

    def get(self, cache_key: str) -> dict[str, Any] | None:
        now = self._clock()
        wall_now = _as_utc(self._wall_clock())
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                return None
            monotonic_expiry, absolute_expiry, value = entry
            if monotonic_expiry <= now or absolute_expiry <= wall_now:
                self._entries.pop(cache_key, None)
                return None
            self._entries.move_to_end(cache_key)
            # 调用方会为当前响应重新整理形状；返回副本，避免一次请求给候选添加
            # 字段后污染其他请求看到的缓存对象。
            return copy.deepcopy(value)

    def expires_at_for(self, checked_at: str) -> str:
        checked = _parse_utc_z(checked_at)
        return _format_utc_z(checked + timedelta(seconds=self._ttl))

    def set(
        self,
        cache_key: str,
        value: dict[str, Any],
        *,
        checked_at: str | None = None,
    ) -> str:
        completion = value.get("completion") if isinstance(value, dict) else None
        reuse = value.get("reuse") if isinstance(value, dict) else None
        if (
            not isinstance(completion, dict)
            or completion.get("status") != "complete"
            or not isinstance(reuse, dict)
            or reuse.get("policy") != "allowed"
        ):
            raise ValueError("内部缓存只接受完整且允许复用的查重结果。")
        now = self._clock()
        wall_now = _as_utc(self._wall_clock())
        if checked_at is None:
            checked_value = value.get("checkedAt")
            checked = (
                _parse_utc_z(checked_value)
                if isinstance(checked_value, str)
                else wall_now
            )
        else:
            checked = _parse_utc_z(checked_at)
        absolute_expiry = checked + timedelta(seconds=self._ttl)
        # 墙钟若在 checkedAt 生成后向后校正，也不能让进程内实际存活时间超过
        # 配置 TTL；向前校正则按绝对 expiresAt 提前失效。
        remaining = max(
            0.0,
            min(float(self._ttl), (absolute_expiry - wall_now).total_seconds()),
        )
        expires_at = _format_utc_z(absolute_expiry)
        with self._lock:
            if remaining <= 0:
                self._entries.pop(cache_key, None)
                return expires_at
            self._entries[cache_key] = (
                now + remaining,
                absolute_expiry,
                copy.deepcopy(value),
            )
            self._entries.move_to_end(cache_key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return expires_at


def _parse_utc_z(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("缓存时间必须是 UTC Z 时间。")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("缓存时间不合法。") from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("缓存时间必须是 UTC 时间。")
    return parsed


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("wall_clock 必须返回带时区的 datetime。")
    return value.astimezone(timezone.utc)


def _format_utc_z(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
