"""按内容摘要缓存查重结果的内存实现。

只缓存已经映射成契约结构的结果，不缓存 yuantiji 的原始响应，也不落盘（题面相关内容
不应长期留存）。到期或超出容量时清理最旧的条目。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class ResultCache:
    def __init__(self, ttl_seconds: int, max_entries: int, clock: Any = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()

    def get(self, content_hash: str) -> dict[str, Any] | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(content_hash)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._entries.pop(content_hash, None)
                return None
            self._entries.move_to_end(content_hash)
            return value

    def set(self, content_hash: str, value: dict[str, Any]) -> None:
        now = self._clock()
        with self._lock:
            self._entries[content_hash] = (now + self._ttl, value)
            self._entries.move_to_end(content_hash)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
