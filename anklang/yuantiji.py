"""yuantiji.ac 反向代理客户端。

把 Urmotiv 的查重请求翻译成 yuantiji.ac 的公开检索请求，再把结果映射回契约里的
candidate 结构。对上游保持克制：串行调用、请求之间留最小间隔，避免给个人自费维护的
服务造成压力。

/api/search 的字段来自对 yuantiji.ac 前端内联 JS 的逆向，并已用一次真实调用确认：
  请求  {query, k, rewrite, skip_short, sources, rerank}
  响应  {results: [{uid, title, url, src, cos, rr, base_rank, also, original, ...}]}
其中 cos 是向量余弦相似度、rr 是重排分数（可能为 null）。相似度优先取 rr，没有再取 cos。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

_MAX_RESPONSE_BYTES = 4_000_000


class YuantijiError(RuntimeError):
    pass


class YuantijiClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        minimum_interval_seconds: float,
        opener: Any | None = None,
    ) -> None:
        self._search_url = f"{base_url}/api/search"
        self._health_url = f"{base_url}/api/health"
        self._timeout = timeout_seconds
        self._minimum_interval = minimum_interval_seconds
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._last_call_monotonic = 0.0

    def health(self) -> dict[str, Any]:
        return self._get(self._health_url)

    def search(self, query: str, k: int, rerank: bool) -> list[dict[str, Any]]:
        body = json.dumps(
            {
                "query": query,
                "k": k,
                "rewrite": False,
                "skip_short": False,
                "sources": [],
                "rerank": rerank,
            }
        ).encode("utf-8")
        payload = self._post(self._search_url, body)
        results = payload.get("results")
        if not isinstance(results, list):
            raise YuantijiError("yuantiji 返回内容缺少 results 列表。")
        return [self._map_candidate(item) for item in results if isinstance(item, dict)]

    @staticmethod
    def _map_candidate(item: dict[str, Any]) -> dict[str, Any]:
        rerank_score = item.get("rr")
        cosine = item.get("cos")
        similarity = None
        if isinstance(rerank_score, (int, float)):
            similarity = float(rerank_score)
        elif isinstance(cosine, (int, float)):
            similarity = float(cosine)
        similarity = 0.0 if similarity is None else max(0.0, min(1.0, similarity))

        original = item.get("original")
        explanation = None
        if isinstance(original, str) and original.strip():
            snippet = " ".join(original.split())
            explanation = f"候选题面片段：{snippet[:400]}"

        return {
            "source": str(item.get("src") or "yuantiji"),
            "externalId": str(item.get("uid") or ""),
            "title": str(item.get("title") or "未命名题目"),
            "url": item.get("url") if isinstance(item.get("url"), str) else None,
            "similarity": similarity,
            "explanation": explanation,
        }

    def _throttle(self) -> None:
        if self._minimum_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call_monotonic
        wait = self._minimum_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        return self._perform(request)

    def _post(self, url: str, body: bytes) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return self._perform(request)

    def _perform(self, request: urllib.request.Request) -> dict[str, Any]:
        with self._lock:
            self._throttle()
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
            except urllib.error.URLError as error:
                raise YuantijiError("无法连接 yuantiji 服务。") from error
            except TimeoutError as error:
                raise YuantijiError("yuantiji 服务响应超时。") from error
            finally:
                self._last_call_monotonic = time.monotonic()

        if len(raw) > _MAX_RESPONSE_BYTES:
            raise YuantijiError("yuantiji 返回内容过大。")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise YuantijiError("yuantiji 返回内容不是有效 JSON。") from error
        if not isinstance(parsed, dict):
            raise YuantijiError("yuantiji 返回内容不是 JSON 对象。")
        return parsed
