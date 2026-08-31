"""Anklang search entrypoint, adapted from is-my-problem-new v2 ``ui/server.py``.

Upstream: https://github.com/fjzzq2002/is-my-problem-new/blob/72e309bdcea2669bc3f476bea6fa81b1f21e788a/ui/server.py
Copyright (c) 2023 Ziqian Zhong, MIT License.

The preserved upstream path is:

``query vector -> cosine_all -> descending order -> collapse -> mkrow``.

Only the configured embedding provider, incremental SQLite rows, and Urmotiv's versioned
machine HTTP adapter differ. Upstream rewriting, reranking, query-vector cache, statistics,
and SPA routes are intentionally absent because Anklang only returns ranked candidates.
"""
from __future__ import annotations

import signal
import threading
import time
from collections.abc import Sequence
from typing import Any

from anklang.backends import (
    BackendError,
    BackendSearchResult,
    BackendUpsertResult,
    SearchBackend,
)
from anklang.config import AppConfig
from anklang.embedding import EmbeddingClient, EmbeddingError
from anklang.http_api import (
    AnklangHTTPServer,
    AnklangService,
    ServiceRuntime,
    make_handler,
)
from anklang.ingest import UpsertUnavailable, ingest_once, upsert_one_problem
from anklang.provider import ProviderConfig, ProviderRegistry
from anklang.search_sources import (
    ConfiguredSearchBackend,
    SearchSourceConfig,
    SearchSourceRegistry,
)
from anklang.sources import RawProblem
from anklang.store import EmbeddingIndexSpec, IndexMetadataError, ProblemStore, StoredProblem
from anklang.vectormath import cosine_similarity


def cosine_all(query_vector: Sequence[float], problems: Sequence[StoredProblem]) -> list[float]:
    """Preserved upstream all-row cosine pass, using the standard-library vector helper."""

    similarities: list[float] = []
    for problem in problems:
        if problem.embedding is None:
            raise ValueError("向量快照不完整。")
        raw = cosine_similarity(list(query_vector), problem.embedding)
        similarities.append(max(0.0, min(1.0, (raw + 1.0) / 2.0)))
    return similarities


def collapse(
    order_idx: Sequence[int],
    similarities: Sequence[float],
    problems: Sequence[StoredProblem],
    limit: int,
) -> list[tuple[StoredProblem, float]]:
    """Preserved upstream ranked walk; stable source IDs replace offline duplicate groups."""

    kept: list[tuple[StoredProblem, float]] = []
    seen: set[tuple[str, str]] = set()
    for index in order_idx:
        problem = problems[index]
        identity = (problem.source, problem.external_id)
        if identity in seen:
            continue
        seen.add(identity)
        kept.append((problem, float(similarities[index])))
        if len(kept) >= limit:
            break
    return kept


def mkrow(problem: StoredProblem, similarity: float) -> dict[str, Any]:
    """Preserved upstream row projection, narrowed to the public candidate contract."""

    row: dict[str, Any] = {
        "source": problem.source,
        "externalId": problem.external_id,
        "title": problem.title,
        "similarity": round(float(similarity), 6),
    }
    if problem.url:
        row["url"] = problem.url
    if problem.metadata:
        row["metadata"] = problem.metadata
    statement, truncated = _candidate_statement(problem.statement)
    row["statement"] = statement
    if truncated:
        row["statementTruncated"] = True
    return row


def _candidate_statement(value: str) -> tuple[str, bool]:
    maximum = 32_000
    current = 0
    kept: list[str] = []
    for character in value.strip():
        width = 2 if ord(character) > 0xFFFF else 1
        if current + width > maximum:
            return "".join(kept), True
        kept.append(character)
        current += width
    return "".join(kept), False


def search(
    store: ProblemStore,
    embedder: EmbeddingClient,
    query: str,
    k: int,
) -> list[dict[str, Any]]:
    """Run the upstream v2 search data flow against the current incremental snapshot."""

    snapshot = store.search_snapshot(
        EmbeddingIndexSpec(embedder.model, embedder.dimensions)
    )
    if not snapshot.vector_ready:
        raise IndexMetadataError(snapshot.vector_status, "本地向量索引不可用。")
    query_vector = embedder.embed_one(query)
    similarities = cosine_all(query_vector, snapshot.problems)
    order = sorted(range(len(similarities)), key=similarities.__getitem__, reverse=True)
    ranked = collapse(order, similarities, snapshot.problems, k)
    return [mkrow(problem, score) for problem, score in ranked]


class UpstreamSearchBackend(SearchBackend):
    """Runtime backend whose search method delegates to the preserved upstream flow above.

    The active embedding provider lives in a thread-safe in-memory registry; every
    embedding operation acquires the current client and releases it afterwards, so the
    provider can be replaced or cleared at runtime without a restart.
    """

    def __init__(
        self,
        store: ProblemStore,
        provider: ProviderRegistry | None = None,
    ) -> None:
        self.store = store
        self.provider = provider if provider is not None else ProviderRegistry()
        self._rebuild_lock = threading.RLock()
        self._rebuild_generation = 0
        self._rebuild_thread: threading.Thread | None = None
        self._rebuild_state = "idle"
        self._rebuild_processed = 0
        self._rebuild_total = 0
        self._rebuild_reason: str | None = None
        self._pending_config: ProviderConfig | None = None

    @property
    def index_spec(self) -> EmbeddingIndexSpec | None:
        return self.provider.spec()

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        if self.embedding_rebuild_status()["state"] == "running":
            return BackendSearchResult.unavailable(
                reason_code="search_backend_unavailable",
                retryable=True,
                retry_after_seconds=1,
            )
        client = self.provider.acquire()
        if client is None:
            return BackendSearchResult.unavailable(
                reason_code="search_backend_unavailable",
                retryable=False,
            )
        try:
            candidates = search(self.store, client, query_text, k)
        except EmbeddingError:
            return BackendSearchResult.unavailable(
                reason_code="search_backend_unavailable",
                retryable=True,
            )
        except IndexMetadataError as error:
            return BackendSearchResult.unavailable(
                reason_code=(
                    "search_backend_invalid"
                    if error.status != "empty"
                    else "search_backend_unavailable"
                ),
                retryable=False,
            )
        except (BackendError, ValueError, OSError):
            return BackendSearchResult.unavailable(
                reason_code="search_backend_unavailable",
                retryable=False,
            )
        finally:
            self.provider.release()
        return BackendSearchResult(candidates)

    def upsert_problem(
        self,
        external_id: str,
        title: str,
        basic_statement: str,
        updated_at: str,
    ) -> BackendUpsertResult:
        """把固定来源 ``urmotiv`` 的一道题写入同一 SQLite/向量路径。"""

        if self.embedding_rebuild_status()["state"] == "running":
            raise BackendError(
                reason_code="service_unavailable",
                retryable=True,
                retry_after_seconds=1,
            )
        client = self.provider.acquire()
        if client is None:
            raise BackendError(
                reason_code="service_unavailable",
                retryable=False,
            )
        try:
            try:
                result = upsert_one_problem(
                    self.store,
                    client,
                    RawProblem(
                        external_id=external_id,
                        title=title,
                        statement=basic_statement,
                        updated_at=updated_at,
                    ),
                    source="urmotiv",
                )
            except UpsertUnavailable as error:
                raise BackendError(
                    reason_code="service_unavailable",
                    retryable=True,
                ) from error
            except Exception as error:
                # 只向 HTTP 层传播固定类别，避免数据库/提供方细节越过边界。
                raise BackendError(
                    reason_code="service_unavailable",
                    retryable=True,
                ) from error
        finally:
            self.provider.release()
        return BackendUpsertResult(result.outcome, result.content_hash)

    def configure_provider(self, config: ProviderConfig) -> None:
        """Install a compatible provider or rebuild every local vector in bounded batches."""

        spec = EmbeddingIndexSpec(config.model, config.dimension)
        with self._rebuild_lock:
            worker = self._rebuild_thread
            if worker is not None and worker.is_alive():
                if self._pending_config == config:
                    return
                raise BackendError(
                    reason_code="service_unavailable",
                    retryable=True,
                    retry_after_seconds=1,
                )
            self._rebuild_thread = None

            if self.provider.matches(config):
                self._rebuild_state = "idle"
                self._rebuild_processed = 0
                self._rebuild_total = 0
                self._rebuild_reason = None
                self._pending_config = None
                return

            if self.store.count() == 0:
                self.store.begin_embedding_rebuild()
                if not self.store.finalize_embedding_rebuild(
                    spec, base_url=config.base_url
                ):
                    raise BackendError(
                        reason_code="service_unavailable", retryable=True
                    )
                self.provider.configure(config)
                self._rebuild_state = "idle"
                self._rebuild_processed = 0
                self._rebuild_total = 0
                self._rebuild_reason = None
                self._pending_config = None
                return

            if self.store.index_matches_provider(spec, config.base_url):
                self.provider.configure(config)
                self._rebuild_state = "idle"
                self._rebuild_processed = 0
                self._rebuild_total = 0
                self._rebuild_reason = None
                self._pending_config = None
                return

            self._rebuild_generation += 1
            generation = self._rebuild_generation
            self._pending_config = config
            self._rebuild_state = "running"
            self._rebuild_processed = 0
            self._rebuild_total = self.store.count()
            self._rebuild_reason = None
            worker = threading.Thread(
                target=self._rebuild_embeddings,
                args=(generation, config),
                daemon=True,
                name="anklang-embedding-rebuild",
            )
            self._rebuild_thread = worker
            worker.start()

    def clear_provider(self) -> None:
        with self._rebuild_lock:
            self._rebuild_generation += 1
            self._pending_config = None
            worker = self._rebuild_thread
            if worker is not None and worker.is_alive():
                self._rebuild_state = "running"
                self._rebuild_reason = None
            else:
                worker = None
                self._rebuild_thread = None
                self._rebuild_state = "idle"
                self._rebuild_processed = 0
                self._rebuild_total = 0
                self._rebuild_reason = None

        # Block new uses of the registered client immediately. A rebuild owns a separate
        # bounded client, so wait for its current batch to finish before reporting that the
        # credential has been cleared.
        self.provider.clear()
        if worker is None:
            return
        worker.join(timeout=35)
        if worker.is_alive():
            with self._rebuild_lock:
                self._rebuild_state = "failed"
                self._rebuild_reason = "configuration_clear_incomplete"
            raise BackendError(
                reason_code="service_unavailable",
                retryable=True,
                retry_after_seconds=1,
            )
        try:
            self.store.abort_embedding_rebuild()
        except Exception as error:
            with self._rebuild_lock:
                self._rebuild_state = "failed"
                self._rebuild_reason = "configuration_clear_incomplete"
                self._rebuild_thread = None
            raise BackendError(
                reason_code="service_unavailable", retryable=True
            ) from error
        with self._rebuild_lock:
            self._rebuild_thread = None
            self._rebuild_state = "idle"
            self._rebuild_processed = 0
            self._rebuild_total = 0
            self._rebuild_reason = None

    def embedding_rebuild_status(self) -> dict[str, Any]:
        with self._rebuild_lock:
            status: dict[str, Any] = {
                "state": self._rebuild_state,
                "processed": self._rebuild_processed,
                "total": self._rebuild_total,
            }
            if self._rebuild_reason is not None:
                status["reasonCode"] = self._rebuild_reason
            return status

    def _rebuild_embeddings(self, generation: int, config: ProviderConfig) -> None:
        spec = EmbeddingIndexSpec(config.model, config.dimension)
        client = EmbeddingClient(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            dimensions=config.dimension,
        )
        try:
            total = self.store.begin_embedding_rebuild()
            with self._rebuild_lock:
                if generation != self._rebuild_generation:
                    return
                self._rebuild_total = total
            offset = 0
            while offset < total:
                with self._rebuild_lock:
                    if generation != self._rebuild_generation:
                        self.store.abort_embedding_rebuild()
                        return
                problems = self.store.embedding_rebuild_batch(offset, 10)
                if not problems:
                    break
                vectors = client.embed_batch([problem.statement for problem in problems])
                self.store.stage_embedding_rebuild_batch(
                    [
                        (
                            problem.source,
                            problem.external_id,
                            problem.content_hash,
                            vector,
                        )
                        for problem, vector in zip(problems, vectors, strict=True)
                    ],
                    spec,
                )
                offset += len(problems)
                with self._rebuild_lock:
                    if generation == self._rebuild_generation:
                        self._rebuild_processed = offset
            with self._rebuild_lock:
                if generation != self._rebuild_generation:
                    self.store.abort_embedding_rebuild()
                    return
            if not self.store.finalize_embedding_rebuild(
                spec, base_url=config.base_url
            ):
                raise IndexMetadataError(
                    "source_changed", "重建期间本地题目发生变化。"
                )
            with self._rebuild_lock:
                if generation != self._rebuild_generation:
                    return
                # Keep generation validation, provider installation and the visible state
                # transition under one lock so clear/reconfigure cannot install stale keys
                # after the vector swap.
                self.provider.configure(config, client=client)
                self._rebuild_state = "idle"
                self._rebuild_processed = total
                self._rebuild_total = total
                self._rebuild_reason = None
                self._pending_config = None
                self._rebuild_thread = None
        except Exception:
            try:
                self.store.abort_embedding_rebuild()
            except Exception:
                pass
            with self._rebuild_lock:
                if generation == self._rebuild_generation:
                    self._rebuild_state = "failed"
                    self._rebuild_reason = "embedding_rebuild_failed"
                    self._pending_config = None
                    self._rebuild_thread = None

    def describe_health(self) -> dict[str, Any]:
        configured, _base_url, model, dimension = self.provider.status()
        spec = None
        if configured and model is not None and dimension is not None:
            spec = EmbeddingIndexSpec(model, dimension)
        try:
            inspection = self.store.inspect_index(spec)
        except Exception:
            return {
                "backend": "upstream-v2",
                "localStoreReady": False,
                "localProblemCount": None,
                "embeddingAvailable": configured,
                "vectorIndexReady": False,
                "vectorIndexStatus": "store_unavailable",
                "embeddingRebuild": self.embedding_rebuild_status(),
            }
        return {
            "backend": "upstream-v2",
            "localStoreReady": True,
            "localProblemCount": inspection.problem_count,
            "embeddingAvailable": configured,
            "vectorIndexReady": inspection.vector_ready,
            "vectorIndexStatus": inspection.status,
            "embeddingRebuild": self.embedding_rebuild_status(),
        }

    def close(self) -> None:
        with self._rebuild_lock:
            self._rebuild_generation += 1
            worker = self._rebuild_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=35)
        self.store.close()


def build_backend(config: AppConfig) -> ConfiguredSearchBackend:
    """从环境配置只创建本地存储与空的提供方注册表。

    提供方不由环境变量激活；进程启动后必须由 Urmotiv 通过管理接口重新供给。
    """
    store = ProblemStore(config.local_db_path)
    local = UpstreamSearchBackend(store, ProviderRegistry())
    sources = SearchSourceRegistry(
        SearchSourceConfig(
            mode=config.search_mode,  # type: ignore[arg-type]
            yuantiji_base_url=config.yuantiji_base_url,
            yuantiji_rerank=config.yuantiji_rerank,
        )
    )
    return ConfiguredSearchBackend(local, sources)


def build_service(config: AppConfig) -> AnklangService:
    return AnklangService(config, build_backend(config))


def start_background_ingest(
    backend: ConfiguredSearchBackend,
    config: AppConfig,
    stop_event: threading.Event,
) -> threading.Thread:
    """Poll source plugins in-process; every committed vector is visible to the next search."""

    def _loop() -> None:
        while not stop_event.is_set():
            try:
                client = backend.provider.acquire()
                try:
                    if client is not None:
                        ingest_once(backend.store, client)
                finally:
                    backend.provider.release()
            except Exception:
                pass
            stop_event.wait(config.ingest_interval_seconds)

    thread = threading.Thread(target=_loop, daemon=True, name="anklang-ingest")
    thread.start()
    return thread


def _install_shutdown_handlers(runtime: ServiceRuntime) -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}

    def _request_shutdown(_signum: int, _frame: Any) -> None:
        runtime.begin_shutdown()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _request_shutdown)
    return previous


def _restore_shutdown_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def serve(config: AppConfig) -> None:
    service = build_service(config)
    runtime = ServiceRuntime(config.max_in_flight_checks)
    httpd = AnklangHTTPServer((config.bind_host, config.port), make_handler(service, runtime))
    httpd.timeout = 0.2
    stop_event = threading.Event()
    ingest_thread: threading.Thread | None = None
    if config.ingest_enabled:
        ingest_thread = start_background_ingest(service.backend, config, stop_event)
    previous_handlers = _install_shutdown_handlers(runtime)
    try:
        while runtime.accepting:
            httpd.handle_request()
    except KeyboardInterrupt:
        runtime.begin_shutdown()
    finally:
        runtime.begin_shutdown()
        stop_event.set()
        httpd.server_close()
        deadline = time.monotonic() + config.shutdown_grace_seconds
        runtime.wait_for_idle(config.shutdown_grace_seconds)
        if ingest_thread is not None:
            ingest_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        _restore_shutdown_handlers(previous_handlers)
        service.backend.close()


def main() -> None:
    from anklang.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as error:
        raise SystemExit(str(error)) from error
    serve(config)


if __name__ == "__main__":
    main()
