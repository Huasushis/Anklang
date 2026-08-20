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

from anklang.backends import BackendError, BackendSearchResult, SearchBackend
from anklang.config import AppConfig
from anklang.embedding import EmbeddingClient, EmbeddingError
from anklang.http_api import (
    AnklangHTTPServer,
    AnklangService,
    ServiceRuntime,
    make_handler,
)
from anklang.ingest import ingest_once
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
    return row


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
    """Runtime backend whose search method delegates to the preserved upstream flow above."""

    def __init__(self, store: ProblemStore, embedder: EmbeddingClient | None) -> None:
        self.store = store
        self.embedder = embedder

    @property
    def index_spec(self) -> EmbeddingIndexSpec | None:
        if self.embedder is None:
            return None
        return EmbeddingIndexSpec(self.embedder.model, self.embedder.dimensions)

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        if self.embedder is None:
            return BackendSearchResult.unavailable(
                reason_code="search_backend_unavailable",
                retryable=False,
            )
        try:
            candidates = search(self.store, self.embedder, query_text, k)
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
        return BackendSearchResult(candidates)

    def describe_health(self) -> dict[str, Any]:
        try:
            inspection = self.store.inspect_index(self.index_spec)
        except Exception:
            return {
                "backend": "upstream-v2",
                "localStoreReady": False,
                "localProblemCount": None,
                "embeddingAvailable": self.embedder is not None,
                "vectorIndexReady": False,
                "vectorIndexStatus": "store_unavailable",
            }
        return {
            "backend": "upstream-v2",
            "localStoreReady": True,
            "localProblemCount": inspection.problem_count,
            "embeddingAvailable": self.embedder is not None,
            "vectorIndexReady": inspection.vector_ready,
            "vectorIndexStatus": inspection.status,
        }

    def close(self) -> None:
        self.store.close()


def build_backend(config: AppConfig) -> UpstreamSearchBackend:
    store = ProblemStore(config.local_db_path)
    embedder: EmbeddingClient | None = None
    if config.dashscope_api_key and config.dashscope_base_url:
        embedder = EmbeddingClient(
            base_url=config.dashscope_base_url,
            api_key=config.dashscope_api_key,
            model=config.dashscope_embedding_model,
            dimensions=config.dashscope_embedding_dim,
        )
        try:
            store.prepare_embedding_writes(
                EmbeddingIndexSpec(embedder.model, embedder.dimensions)
            )
        except IndexMetadataError:
            pass
    return UpstreamSearchBackend(store, embedder)


def build_service(config: AppConfig) -> AnklangService:
    return AnklangService(config, build_backend(config))


def start_background_ingest(
    backend: UpstreamSearchBackend,
    config: AppConfig,
    stop_event: threading.Event,
) -> threading.Thread:
    """Poll source plugins in-process; every committed vector is visible to the next search."""

    def _loop() -> None:
        while not stop_event.is_set():
            try:
                ingest_once(backend.store, backend.embedder)
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
