"""本地索引缓存命中窗口的二次身份门禁；全部使用合成数据。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from anklang.backends.local_engine import LocalEngineBackend
from anklang.cache import ResultCache
from anklang.config import AppConfig
from anklang.contracts import parse_request
from anklang.review import evaluate
from anklang.server import AnklangService
from anklang.store import ProblemStore


class _StaticEmbedder:
    model = "synthetic-model"
    dimensions = 2

    def __init__(self) -> None:
        self.calls = 0

    def embed_one(self, _text: str) -> list[float]:
        self.calls += 1
        return [1.0, 0.0]


def _config(path: Path) -> AppConfig:
    return AppConfig(
        port=8730,
        service_token="synthetic-service-token",
        yuantiji_base_url="https://upstream.example.invalid",
        yuantiji_timeout_seconds=12.0,
        yuantiji_minimum_interval_seconds=0.0,
        search_k=8,
        use_rerank=False,
        minimum_similarity=0.1,
        block_threshold=0.9,
        cache_ttl_seconds=3_600,
        cache_max_entries=100,
        llm_review_enabled=False,
        llm_base_url=None,
        llm_api_key=None,
        llm_model="synthetic-review-model",
        llm_review_top_n=2,
        llm_timeout_seconds=10.0,
        backend="local_engine",
        local_db_path=str(path),
    )


def _request() -> dict[str, Any]:
    return parse_request(
        {
            "apiVersion": "2",
            "requestId": "55555555-5555-4555-8555-555555555555",
            "contentHash": "5" * 64,
            "problem": {
                "title": "合成缓存测试",
                "type": "traditional",
                "tagIds": ["synthetic"],
                "basicStatement": "合成数组 alpha beta",
            },
        },
        expected_api_version="2",
    )


class LocalCacheRevalidationTests(unittest.TestCase):
    def _service(
        self, path: Path
    ) -> tuple[AnklangService, LocalEngineBackend, ProblemStore, ResultCache]:
        store = ProblemStore(str(path))
        self.addCleanup(store.close)
        embedder = _StaticEmbedder()
        backend = LocalEngineBackend(store, embedder)  # type: ignore[arg-type]
        assert backend.index_spec is not None
        store.prepare_embedding_writes(backend.index_spec)
        store.add_problem(
            source="synthetic",
            external_id="one",
            title="合成候选一",
            statement="合成数组 alpha beta candidate",
            content_hash="first-hash",
            embedding=[1.0, 0.0],
            index_spec=backend.index_spec,
            source_updated_at="2026-08-01T00:00:00.000Z",
        )
        config = _config(path)
        cache = ResultCache(config.cache_ttl_seconds, config.cache_max_entries)
        return AnklangService(config, backend, cache, None), backend, store, cache

    def test_stable_hit_rechecks_o1_identity_without_search_or_evaluate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, backend, store, _cache = self._service(
                Path(directory) / "stable.db"
            )
            with patch.object(
                backend, "search", wraps=backend.search
            ) as search_spy, patch(
                "anklang.server.evaluate", wraps=evaluate
            ) as evaluate_spy:
                first = service.check_similarity(_request(), api_version="2")
                traced: list[str] = []
                store._conn.set_trace_callback(traced.append)  # noqa: SLF001
                second = service.check_similarity(_request(), api_version="2")
                store._conn.set_trace_callback(None)  # noqa: SLF001

            self.assertEqual(first, second)
            self.assertEqual(search_spy.call_count, 1)
            self.assertEqual(evaluate_spy.call_count, 1)
            normalized_sql = " ".join(traced).lower()
            self.assertNotIn(" from problems", normalized_sql)
            self.assertNotIn("statement", normalized_sql)
            self.assertNotIn("select embedding", normalized_sql)
            self.assertGreaterEqual(normalized_sql.count("pragma data_version"), 2)

    def test_unvectorized_write_during_cache_get_cannot_return_old_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, backend, store, cache = self._service(
                Path(directory) / "incomplete.db"
            )
            first = service.check_similarity(_request(), api_version="2")
            self.assertEqual(first["completion"]["status"], "complete")
            original_get = cache.get
            mutated = False

            def _get_then_add(cache_key: str) -> dict[str, Any] | None:
                nonlocal mutated
                value = original_get(cache_key)
                if value is not None and not mutated:
                    mutated = True
                    assert backend.index_spec is not None
                    store.add_problem(
                        source="synthetic",
                        external_id="two",
                        title="合成候选二",
                        statement="合成数组 alpha beta second",
                        content_hash="second-hash",
                        embedding=None,
                        index_spec=backend.index_spec,
                    )
                return value

            with patch.object(cache, "get", side_effect=_get_then_add), patch.object(
                backend, "search", wraps=backend.search
            ) as search_spy:
                second = service.check_similarity(_request(), api_version="2")

            self.assertTrue(mutated)
            self.assertEqual(search_spy.call_count, 1)
            self.assertEqual(second["completion"]["status"], "partial")
            self.assertEqual(second["reuse"], {"policy": "no-store"})
            self.assertNotEqual(second, first)

    def test_external_metadata_damage_during_cache_get_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.db"
            service, backend, _store, cache = self._service(path)
            first = service.check_similarity(_request(), api_version="2")
            self.assertEqual(first["completion"]["status"], "complete")
            original_get = cache.get
            mutated = False

            def _get_then_damage(cache_key: str) -> dict[str, Any] | None:
                nonlocal mutated
                value = original_get(cache_key)
                if value is not None and not mutated:
                    mutated = True
                    external = sqlite3.connect(path)
                    try:
                        external.execute(
                            "DELETE FROM index_metadata WHERE key = 'schema_version'"
                        )
                        external.commit()
                    finally:
                        external.close()
                return value

            with patch.object(cache, "get", side_effect=_get_then_damage), patch.object(
                backend, "search", wraps=backend.search
            ) as search_spy:
                second = service.check_similarity(_request(), api_version="2")

            self.assertTrue(mutated)
            self.assertEqual(search_spy.call_count, 1)
            self.assertNotEqual(second["completion"]["status"], "complete")
            self.assertEqual(second["reuse"], {"policy": "no-store"})
            self.assertNotEqual(second, first)

    def test_same_connection_change_then_restore_still_advances_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, backend, store, cache = self._service(
                Path(directory) / "local-aba.db"
            )
            service.check_similarity(_request(), api_version="2")
            original_get = cache.get

            def _get_then_restore(cache_key: str) -> dict[str, Any] | None:
                value = original_get(cache_key)
                if value is not None:
                    assert backend.index_spec is not None
                    first_update = store.add_problem(
                        source="synthetic",
                        external_id="one",
                        title="合成临时标题",
                        statement="合成数组 alpha beta candidate",
                        content_hash="first-hash",
                        embedding=[1.0, 0.0],
                        index_spec=backend.index_spec,
                        source_updated_at="2026-08-01T00:00:01.000Z",
                    )
                    restored = store.add_problem(
                        source="synthetic",
                        external_id="one",
                        title="合成候选一",
                        statement="合成数组 alpha beta candidate",
                        content_hash="first-hash",
                        embedding=[1.0, 0.0],
                        index_spec=backend.index_spec,
                        source_updated_at="2026-08-01T00:00:02.000Z",
                    )
                    self.assertEqual(
                        (first_update, restored), ("updated", "updated")
                    )
                return value

            with patch.object(cache, "get", side_effect=_get_then_restore), patch.object(
                backend, "search", wraps=backend.search
            ) as search_spy:
                result = service.check_similarity(_request(), api_version="2")

            self.assertEqual(search_spy.call_count, 1)
            self.assertEqual(result["completion"]["status"], "complete")

    def test_external_metadata_change_then_restore_still_advances_data_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "external-aba.db"
            service, backend, _store, cache = self._service(path)
            service.check_similarity(_request(), api_version="2")
            original_get = cache.get

            def _get_then_restore(cache_key: str) -> dict[str, Any] | None:
                value = original_get(cache_key)
                if value is not None:
                    external = sqlite3.connect(path)
                    try:
                        original = external.execute(
                            "SELECT value FROM index_metadata WHERE key = 'schema_version'"
                        ).fetchone()
                        assert original is not None
                        external.execute(
                            "UPDATE index_metadata SET value = 'temporary' "
                            "WHERE key = 'schema_version'"
                        )
                        external.commit()
                        external.execute(
                            "UPDATE index_metadata SET value = ? "
                            "WHERE key = 'schema_version'",
                            (original[0],),
                        )
                        external.commit()
                    finally:
                        external.close()
                return value

            with patch.object(cache, "get", side_effect=_get_then_restore), patch.object(
                backend, "search", wraps=backend.search
            ) as search_spy:
                result = service.check_similarity(_request(), api_version="2")

            self.assertEqual(search_spy.call_count, 1)
            self.assertEqual(result["completion"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
