"""检索后端抽象：把"怎么找相似题目"这件事从 AnklangService 里抽出来，做成可以
互相替换的统一接口。目前有两种实现：

  - reverse_proxy.ReverseProxyBackend  转发给 yuantiji.ac（阶段 1，生产环境默认）。
  - local_engine.LocalEngineBackend    本地题库的向量 + 关键词混合检索（阶段 2，
                                        面向未来自建题库场景，默认不启用）。

由 anklang/config.py 的 ANKLANG_BACKEND 配置项决定 anklang/server.py 实例化哪一个；
两者对上层（AnklangService、anklang.review.evaluate）暴露完全一样的接口，互相替换
不需要改动查重判定逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


CompletionStatus = Literal["complete", "partial", "unavailable"]
CompletionReason = Literal[
    "complete",
    "search_timeout",
    "search_rate_limited",
    "search_backend_unavailable",
    "search_backend_invalid",
    "search_partial",
    "review_unavailable",
    "service_unavailable",
    "service_invalid_response",
    "internal_error",
]

_NONCOMPLETE_REASONS = {
    "search_timeout",
    "search_rate_limited",
    "search_backend_unavailable",
    "search_backend_invalid",
    "search_partial",
    "review_unavailable",
    "service_unavailable",
    "service_invalid_response",
    "internal_error",
}


class BackendError(RuntimeError):
    """后端连一份可信的结构化结果也无法形成。

    异常文本只供进程内调试，HTTP 层绝不回显；固定原因和重试信息才允许进入 v2
    契约。预期的远程故障通常由后端直接返回 ``BackendSearchResult.unavailable``，
    这个异常主要兜住本地存储等意外失败。
    """

    def __init__(
        self,
        message: str = "检索后端不可用。",
        *,
        reason_code: CompletionReason = "service_unavailable",
        retryable: bool = True,
        retry_after_seconds: int | None = None,
    ) -> None:
        if reason_code not in _NONCOMPLETE_REASONS:
            raise ValueError("BackendError 必须使用非完整原因码。")
        if not isinstance(retryable, bool):
            raise ValueError("BackendError 的重试状态不合法。")
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, int)
            or not 1 <= retry_after_seconds <= 86_400
        ):
            raise ValueError("retry_after_seconds 不合法。")
        if retry_after_seconds is not None and not retryable:
            raise ValueError("不可重试的错误不能带重试等待时间。")
        self.reason_code = reason_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


@dataclass(frozen=True)
class BackendSearchResult:
    """一次后端检索的内部结果，不会直接作为 HTTP 响应发送。

    ``status`` 明确区分完整、部分完成和不可用，避免旧 ``degraded`` 布尔值把
    “仍有可信关键词候选”和“什么都没查到”混在一起。非完整结果绝不进入缓存；
    部分结果可以展示候选，但不能仅凭相似度阈值自动拦截。
    """

    candidates: list[dict[str, Any]]
    status: CompletionStatus = "complete"
    reason_code: CompletionReason = "complete"
    retryable: bool = False
    retry_after_seconds: int | None = None
    # 本地索引成功结果所属的机器身份摘要；远程后端保持 None。服务层只会在
    # 当前 O(1) 门禁仍返回同一摘要时缓存，避免跨语料或跨模型复用旧判断。
    cache_identity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, list):
            raise ValueError("后端候选必须是列表。")
        if not isinstance(self.retryable, bool):
            raise ValueError("后端重试状态必须是布尔值。")
        if self.cache_identity is not None and (
            not isinstance(self.cache_identity, str) or not self.cache_identity
        ):
            raise ValueError("后端缓存身份必须是文本。")
        if self.status == "complete":
            if self.reason_code != "complete" or self.retryable:
                raise ValueError("完整检索必须使用 complete/不可重试状态。")
            if self.retry_after_seconds is not None:
                raise ValueError("完整检索不能带重试等待时间。")
        elif self.status in {"partial", "unavailable"}:
            if self.reason_code not in _NONCOMPLETE_REASONS:
                raise ValueError("非完整检索的原因码不合法。")
            if self.status == "unavailable" and self.candidates:
                raise ValueError("不可用检索不能携带候选。")
            if self.cache_identity is not None:
                raise ValueError("非完整检索不能携带缓存身份。")
        else:
            raise ValueError("检索完成状态不合法。")
        retry_after = self.retry_after_seconds
        if retry_after is not None and (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, int)
            or not 1 <= retry_after <= 86_400
        ):
            raise ValueError("retry_after_seconds 不合法。")
        if retry_after is not None and not self.retryable:
            raise ValueError("不可重试的结果不能带重试等待时间。")

    @classmethod
    def partial(
        cls,
        candidates: list[dict[str, Any]],
        *,
        reason_code: CompletionReason = "search_partial",
        retryable: bool = True,
        retry_after_seconds: int | None = None,
    ) -> "BackendSearchResult":
        return cls(
            candidates=candidates,
            status="partial",
            reason_code=reason_code,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        reason_code: CompletionReason,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> "BackendSearchResult":
        return cls(
            candidates=[],
            status="unavailable",
            reason_code=reason_code,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )


@runtime_checkable
class SearchBackend(Protocol):
    """所有检索后端必须实现的统一接口。

    search() 返回 BackendSearchResult，其中每个候选 dict 至少包含 source /
    externalId / title / similarity，可以再带 url / explanation —— 字段名和含义与
    anklang.contracts.build_result 期望的候选结构一致。anklang.review.evaluate 会
    在此基础上做相似度阈值判定和可选的 LLM 复核，两种后端不需要、也不应该重复
    实现这部分判定逻辑。
    """

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        """用 query_text（题面文本）检索最相似的最多 k 条候选，不要求调用方再排序。
        遇到自身不可用的情况（上游服务挂了、本地存储读取失败等）应该抛出
        BackendError，而不是返回空列表悄悄吞掉——"完全没有候选"和"这次没查成功"
        对上层的处理方式不同（后者要在 message 里如实说明）。
        """
        ...

    def describe_health(self) -> dict[str, Any]:
        """返回这个后端自身的健康信息（供 /api/v1/health 展示），绝不包含密钥。
        这个方法本身不应该抛出异常——后端自己的调用失败应该被内部捕获，体现成
        状态字段（例如 upstreamReady=False），而不是让健康检查端点本身出错。
        """
        ...
