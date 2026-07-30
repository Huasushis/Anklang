"""阶段 1 反向代理后端：把 SearchBackend 接口包装到已有的 YuantijiClient 上。

这是生产环境的默认后端（ANKLANG_BACKEND=reverse_proxy，且默认值就是它）。这个文件
只做"接口适配"，不改变 anklang/yuantiji.py 里任何限流、字段映射、错误处理的行为——
阶段 1 已经联调通过，这里只是让它能通过统一的 SearchBackend 接口被 AnklangService
调用，本身不含新逻辑。
"""
from __future__ import annotations

from typing import Any

from . import BackendError, BackendSearchResult
from ..yuantiji import YuantijiClient, YuantijiError


class ReverseProxyBackend:
    """把 YuantijiClient 适配成 SearchBackend。"""

    def __init__(self, client: YuantijiClient, use_rerank: bool) -> None:
        self._client = client
        self._use_rerank = use_rerank

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        try:
            candidates = self._client.search(
                query=query_text,
                k=k,
                rerank=self._use_rerank,
            )
        except YuantijiError as error:
            raise BackendError(str(error)) from error
        return BackendSearchResult(candidates=candidates, degraded=False)

    def describe_health(self) -> dict[str, Any]:
        try:
            upstream = self._client.health()
        except YuantijiError:
            return {"upstreamReady": False}
        info: dict[str, Any] = {"upstreamReady": bool(upstream.get("ok"))}
        if isinstance(upstream.get("problems"), int):
            info["upstreamProblemCount"] = upstream["problems"]
        return info
