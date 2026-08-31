"""Runtime-selectable local and yuantiji search sources."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

from .backends import BackendError, BackendSearchResult, BackendUpsertResult
from .yuantiji import YuantijiClient, YuantijiError


SearchMode = Literal["yuantiji", "local", "hybrid"]


@dataclass(frozen=True)
class SearchSourceConfig:
    mode: SearchMode
    yuantiji_base_url: str
    yuantiji_rerank: bool = False


class SearchSourceRegistry:
    """Atomically stores the active non-secret source selection."""

    def __init__(self, initial: SearchSourceConfig) -> None:
        self._lock = threading.RLock()
        self._config = initial
        self._yuantiji = YuantijiClient(initial.yuantiji_base_url)

    def configure(self, config: SearchSourceConfig) -> None:
        with self._lock:
            if config.yuantiji_base_url != self._config.yuantiji_base_url:
                self._yuantiji = YuantijiClient(config.yuantiji_base_url)
            self._config = config

    def snapshot(self) -> tuple[SearchSourceConfig, YuantijiClient]:
        with self._lock:
            return self._config, self._yuantiji

    def status(self) -> dict[str, Any]:
        config, client = self.snapshot()
        result: dict[str, Any] = {
            "mode": config.mode,
            "yuantijiBaseUrl": config.yuantiji_base_url,
            "yuantijiRerank": config.yuantiji_rerank,
        }
        if config.mode in {"yuantiji", "hybrid"}:
            # GET /health and the admin status route must stay local and fast. A live
            # upstream probe belongs to the explicit /test route; expose only a recent
            # cached result when one exists.
            health = client.cached_health()
            if health is not None:
                result["yuantijiReady"] = health.get("ok") is True
                if isinstance(health.get("problems"), int):
                    result["yuantijiProblemCount"] = health["problems"]
        return result

    def test(self, config: SearchSourceConfig) -> dict[str, Any]:
        if config.mode == "local":
            return {"ok": True, "yuantijiReady": None}
        current, current_client = self.snapshot()
        client = (
            current_client
            if current.yuantiji_base_url == config.yuantiji_base_url
            else YuantijiClient(config.yuantiji_base_url)
        )
        health = client.health()
        search_ready = False
        if health.get("ok") is True:
            try:
                client.search(
                    "给定两个整数，输出它们的和。这是 Anklang 连接测试使用的合成题面。",
                    1,
                    rerank=config.yuantiji_rerank,
                )
                search_ready = True
            except YuantijiError:
                search_ready = False
        return {
            "ok": health.get("ok") is True and search_ready,
            "yuantijiReady": health.get("ok") is True,
            **(
                {"yuantijiProblemCount": health["problems"]}
                if isinstance(health.get("problems"), int)
                else {}
            ),
        }


class ConfiguredSearchBackend:
    """Combine the optional local index with the public yuantiji corpus."""

    def __init__(self, local_backend: Any, sources: SearchSourceRegistry) -> None:
        self.local = local_backend
        self.sources = sources
        self.store = local_backend.store
        self.provider = local_backend.provider
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="anklang-search-source"
        )

    @property
    def index_spec(self) -> Any:
        return self.local.index_spec

    def configure_search_sources(self, config: SearchSourceConfig) -> dict[str, Any]:
        self.sources.configure(config)
        return self.sources.status()

    def test_search_sources(self, config: SearchSourceConfig) -> dict[str, Any]:
        return self.sources.test(config)

    def configure_provider(self, config: Any) -> None:
        self.local.configure_provider(config)

    def clear_provider(self) -> None:
        self.local.clear_provider()

    def embedding_rebuild_status(self) -> dict[str, Any]:
        return self.local.embedding_rebuild_status()

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        config, yuantiji = self.sources.snapshot()
        if config.mode == "local":
            return self.local.search(query_text, k)
        if config.mode == "yuantiji":
            return self._search_yuantiji(
                yuantiji, query_text, k, config.yuantiji_rerank
            )

        local_future = self._executor.submit(self.local.search, query_text, k)
        public_future = self._executor.submit(
            self._search_yuantiji,
            yuantiji,
            query_text,
            k,
            config.yuantiji_rerank,
        )
        local = local_future.result()
        public = public_future.result()
        return _merge_results(local, public, maximum=k * 2)

    @staticmethod
    def _search_yuantiji(
        client: YuantijiClient,
        query_text: str,
        k: int,
        rerank: bool,
    ) -> BackendSearchResult:
        try:
            result = client.search(query_text, k, rerank=rerank)
        except YuantijiError as error:
            return BackendSearchResult.unavailable(
                reason_code=error.reason_code,
                retryable=error.retryable,
                retry_after_seconds=error.retry_after_seconds,
            )
        if result.partial:
            return BackendSearchResult.partial(
                result.candidates,
                reason_code="search_backend_invalid",
                retryable=False,
            )
        return BackendSearchResult(result.candidates)

    def upsert_problem(
        self,
        external_id: str,
        title: str,
        basic_statement: str,
        updated_at: str,
    ) -> BackendUpsertResult:
        return self.local.upsert_problem(
            external_id, title, basic_statement, updated_at
        )

    def describe_health(self) -> dict[str, Any]:
        info = dict(self.local.describe_health())
        try:
            source_status = self.sources.status()
        except Exception:
            source_status = {"yuantijiReady": False}
        info.update(
            {
                "searchMode": source_status.get("mode"),
                "yuantijiReady": source_status.get("yuantijiReady"),
                "yuantijiProblemCount": source_status.get("yuantijiProblemCount"),
            }
        )
        return info

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.local.close()


def _merge_results(
    first: BackendSearchResult,
    second: BackendSearchResult,
    *,
    maximum: int,
) -> BackendSearchResult:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in sorted(
        [*first.candidates, *second.candidates],
        key=lambda item: float(item.get("similarity", 0.0)),
        reverse=True,
    ):
        identity = (str(candidate.get("source")), str(candidate.get("externalId")))
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(candidate)
        if len(candidates) >= maximum:
            break

    if first.status == "complete" and second.status == "complete":
        return BackendSearchResult(candidates)
    if first.status == "unavailable" and second.status == "unavailable":
        return BackendSearchResult.unavailable(
            reason_code=(
                "search_timeout"
                if "search_timeout" in {first.reason_code, second.reason_code}
                else "search_backend_unavailable"
            ),
            retryable=first.retryable or second.retryable,
            retry_after_seconds=first.retry_after_seconds or second.retry_after_seconds,
        )
    return BackendSearchResult.partial(
        candidates,
        reason_code="search_partial",
        retryable=first.retryable or second.retryable,
        retry_after_seconds=first.retry_after_seconds or second.retry_after_seconds,
    )
