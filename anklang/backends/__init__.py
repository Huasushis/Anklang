"""HTTP adapter boundary for the preserved upstream ``ui.server`` search entrypoint."""
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

    ``status`` 明确区分完整、部分完成和不可用：部分结果可以携带候选，
    不可用结果不能伪装成空的完整查询。
    """

    candidates: list[dict[str, Any]]
    status: CompletionStatus = "complete"
    reason_code: CompletionReason = "complete"
    retryable: bool = False
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, list):
            raise ValueError("后端候选必须是列表。")
        if not isinstance(self.retryable, bool):
            raise ValueError("后端重试状态必须是布尔值。")
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


UpsertOutcome = Literal["inserted", "updated", "unchanged", "stale"]


@dataclass(frozen=True)
class BackendUpsertResult:
    """单题入库的内部结果，不直接作为 HTTP 响应发送。"""

    outcome: UpsertOutcome
    content_hash: str

    def __post_init__(self) -> None:
        if self.outcome not in {"inserted", "updated", "unchanged", "stale"}:
            raise ValueError("入库结果状态不合法。")
        if not isinstance(self.content_hash, str):
            raise TypeError("入库结果哈希类型不合法。")


@runtime_checkable
class SearchBackend(Protocol):
    """所有检索后端必须实现的统一接口。

    search() 返回 BackendSearchResult，其中每个候选 dict 包含 source /
    externalId / title / similarity，可选 url；字段名与
    anklang.contracts.build_result 期望的候选结构一致。
    """

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        """用 query_text（题面文本）检索最相似的最多 k 条候选，不要求调用方再排序。
        遇到自身不可用的情况（本地存储读取失败等）应该抛出 BackendError，而不是
        返回空列表悄悄吞掉；完整空结果和查询失败必须可区分。
        """
        ...

    def describe_health(self) -> dict[str, Any]:
        """返回这个后端自身的健康信息（供 /api/v1/health 展示），绝不包含密钥。
        这个方法本身不应该抛出异常——后端自己的调用失败应该被内部捕获，体现成
        状态字段（例如 embeddingAvailable=False），而不是让健康检查端点本身出错。
        """
        ...
