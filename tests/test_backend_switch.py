"""验证 ANKLANG_BACKEND 配置驱动的后端切换。

1. build_backend() 按配置选出正确的后端类型（reverse_proxy / local_engine），
   没配置百炼凭据时本地引擎优雅降级为"embedder=None"，不报错。
2. 选中 local_engine 后端时，服务器整条 HTTP 链路（鉴权、契约校验、健康检查）与
   阶段 1 的 reverse_proxy 模式一样能跑通——用注入的假 embedding 和预先写入的
   题库，不做真实网络调用。
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from anklang.backends import BackendSearchResult
from anklang.backends.local_engine import LocalEngineBackend
from anklang.backends.reverse_proxy import ReverseProxyBackend
from anklang.cache import ResultCache
from anklang.config import AppConfig
from anklang.embedding import EmbeddingClient
from anklang.server import AnklangService, build_backend, make_handler
from anklang.store import EmbeddingIndexSpec, ProblemStore
from anklang.vectormath import pack_embedding

_INDEX_SPEC = EmbeddingIndexSpec("text-embedding-v4", 2)


def _config(**overrides: Any) -> AppConfig:
    base = dict(
        port=8730,
        service_token="service-token-abcdef123456",
        yuantiji_base_url="https://yuantiji.test",
        yuantiji_timeout_seconds=30.0,
        yuantiji_minimum_interval_seconds=0.0,
        search_k=8,
        use_rerank=False,
        minimum_similarity=0.1,
        block_threshold=0.9,
        cache_ttl_seconds=3600,
        cache_max_entries=100,
        llm_review_enabled=False,
        llm_base_url=None,
        llm_api_key=None,
        llm_model="deepseek-v4-flash",
        llm_review_top_n=2,
        llm_timeout_seconds=30.0,
    )
    base.update(overrides)
    return AppConfig(**base)


def _request(content_hash: str | None = None, statement: str = "给定 n 个整数，输出它们的和。") -> dict[str, Any]:
    return {
        "apiVersion": "1",
        "requestId": "22222222-2222-4222-8222-222222222222",
        "contentHash": content_hash or ("d" * 64),
        "problem": {
            "title": "数组求和",
            "type": "traditional",
            "tagIds": ["math.basic"],
            "basicStatement": statement,
        },
    }


class _FakeEmbeddingOpener:
    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = vectors_by_text
        self.calls = 0

    def __call__(self, request: Any, timeout: float) -> Any:  # noqa: ARG002
        self.calls += 1
        body = json.loads(request.data.decode("utf-8"))
        raw_input = body["input"]
        texts = raw_input if isinstance(raw_input, list) else [raw_input]
        vectors = [self._vectors_by_text[text] for text in texts]
        payload = {
            "model": "text-embedding-v4",
            "data": [{"embedding": vector} for vector in vectors],
        }
        response_body = json.dumps(payload).encode("utf-8")

        class _Response:
            def __enter__(self_inner) -> "_Response":
                return self_inner

            def __exit__(self_inner, *_args: Any) -> None:
                return None

            def read(self_inner, _limit: int) -> bytes:
                return response_body

        return _Response()


class _FakeYuantijiClient:
    def search(self, *, query: str, k: int, rerank: bool) -> list[dict[str, Any]]:
        self.last_search = (query, k, rerank)
        return [
            {
                "source": "unit-test",
                "externalId": "one",
                "title": "示例候选",
                "similarity": 0.8,
            }
        ]

    def health(self) -> dict[str, Any]:
        return {"ok": True}


class _FailingEmbeddingOpener:
    def __init__(self, external_error: str) -> None:
        self.external_error = external_error
        self.calls = 0

    def __call__(self, _request: Any, timeout: float) -> Any:  # noqa: ARG002
        self.calls += 1
        raise OSError(self.external_error)


class _ServerHarness:
    """在回环地址上真实启动一个短命 HTTP server，用标准库 http.client 请求它。"""

    def __init__(self, service: AnklangService) -> None:
        from http.server import ThreadingHTTPServer
        import threading

        handler_class = make_handler(service)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def request(
        self, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body or None, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return response.status, payload
        finally:
            conn.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class BuildBackendTests(unittest.TestCase):
    def test_reverse_proxy_search_result_is_not_degraded(self) -> None:
        client = _FakeYuantijiClient()
        backend = ReverseProxyBackend(client, use_rerank=False)  # type: ignore[arg-type]

        search_result = backend.search("自编测试题面", 3)

        self.assertIsInstance(search_result, BackendSearchResult)
        self.assertFalse(search_result.degraded)
        self.assertEqual(search_result.candidates[0]["externalId"], "one")
        self.assertEqual(client.last_search, ("自编测试题面", 3, False))

    def test_default_config_selects_reverse_proxy(self) -> None:
        config = _config()
        backend = build_backend(config)
        self.assertIsInstance(backend, ReverseProxyBackend)

    def test_local_engine_config_selects_local_engine_backend(self) -> None:
        config = _config(backend="local_engine", local_db_path=":memory:")
        backend = build_backend(config)
        self.assertIsInstance(backend, LocalEngineBackend)
        self.assertIsNone(backend.embedder)  # 没配置 DASHSCOPE_* 时应该优雅降级为 None
        health = backend.describe_health()
        self.assertEqual(health["vectorIndexStatus"], "empty_corpus")
        self.assertFalse(health["indexMetadataReady"])
        backend.store.close()

    def test_local_engine_with_dashscope_credentials_builds_embedder(self) -> None:
        config = _config(
            backend="local_engine",
            local_db_path=":memory:",
            dashscope_base_url="https://dashscope.test/compatible-mode/v1",
            dashscope_api_key="test-key",
        )
        backend = build_backend(config)
        self.assertIsInstance(backend, LocalEngineBackend)
        self.assertIsNotNone(backend.embedder)
        health = backend.describe_health()
        self.assertEqual(health["vectorIndexStatus"], "empty_corpus")
        self.assertFalse(health["vectorIndexReady"])
        backend.store.close()


class LocalEngineServerFlowTests(unittest.TestCase):
    def test_empty_corpus_is_incomplete_with_or_without_embedder(self) -> None:
        for embedding_enabled in (False, True):
            with self.subTest(embedding_enabled=embedding_enabled):
                overrides: dict[str, Any] = {
                    "backend": "local_engine",
                    "local_db_path": ":memory:",
                }
                if embedding_enabled:
                    overrides.update(
                        {
                            "dashscope_base_url": (
                                "https://dashscope.test/compatible-mode/v1"
                            ),
                            "dashscope_api_key": "test-key",
                            "dashscope_embedding_model": "text-embedding-v4",
                            "dashscope_embedding_dim": 2,
                        }
                    )
                config = _config(**overrides)
                backend = build_backend(config)
                assert isinstance(backend, LocalEngineBackend)
                try:
                    service = AnklangService(
                        config,
                        backend,
                        ResultCache(
                            config.cache_ttl_seconds,
                            config.cache_max_entries,
                        ),
                        None,
                    )
                    harness = _ServerHarness(service)
                    try:
                        headers = {
                            "Authorization": (
                                "Bearer service-token-abcdef123456"
                            ),
                            "Content-Type": "application/json",
                        }
                        body = json.dumps(_request()).encode("utf-8")
                        status, payload = harness.request(
                            "POST",
                            "/api/v1/checks/similarity",
                            body,
                            headers,
                        )
                        self.assertEqual(status, 200)
                        self.assertEqual(payload["candidates"], [])
                        self.assertEqual(
                            payload["recommendation"]["message"],
                            "本次未能完成原题检索，请稍后重试并由审题人手工核对。",
                        )
                        self.assertIsNone(service.cache.get("d" * 64))
                        health_status, health = harness.request(
                            "GET", "/api/v1/health"
                        )
                        self.assertEqual(health_status, 200)
                        self.assertEqual(health["status"], "degraded")
                        self.assertEqual(
                            health["vectorIndexStatus"], "empty_corpus"
                        )
                    finally:
                        harness.close()
                finally:
                    backend.store.close()

    def _service(self, db_path: str = ":memory:") -> AnklangService:
        config = _config(backend="local_engine", local_db_path=db_path)
        store = ProblemStore(db_path)
        self.addCleanup(store.close)
        store.prepare_embedding_writes(_INDEX_SPEC)
        store.add_problem(
            source="unit-test",
            external_id="array-sum",
            title="数组求和",
            statement="给定 n 个整数，输出它们的和。",
            content_hash="h1",
            embedding=[1.0, 0.0],
            index_spec=_INDEX_SPEC,
            source_updated_at="2026-01-01T00:00:00.000Z",
        )
        opener = _FakeEmbeddingOpener({"给定 n 个整数，输出它们的和。": [1.0, 0.0]})
        embedder = EmbeddingClient(
            base_url="https://dashscope.test/compatible-mode/v1",
            api_key="test-key",
            model="text-embedding-v4",
            dimensions=2,
            opener=opener,
        )
        backend = LocalEngineBackend(store, embedder, vector_top_k=10, keyword_top_k=10)
        cache = ResultCache(config.cache_ttl_seconds, config.cache_max_entries)
        return AnklangService(config, backend, cache, None)

    def test_similarity_flow_uses_local_engine(self) -> None:
        service = self._service()
        backend = service.backend
        assert isinstance(backend, LocalEngineBackend)
        assert isinstance(backend.embedder, EmbeddingClient)
        opener = backend.embedder._opener
        assert isinstance(opener, _FakeEmbeddingOpener)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {"Authorization": "Bearer service-token-abcdef123456", "Content-Type": "application/json"}
        body = json.dumps(_request(statement="给定 n 个整数，输出它们的和。")).encode("utf-8")

        status, payload = harness.request("POST", "/api/v1/checks/similarity", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["apiVersion"], "1")
        self.assertEqual(payload["candidates"][0]["externalId"], "array-sum")
        self.assertEqual(payload["candidates"][0]["source"], "unit-test")

        second_status, second_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_payload, payload)
        self.assertEqual(opener.calls, 1)

    def test_cached_result_is_rejected_after_index_becomes_incomplete(self) -> None:
        service = self._service()
        backend = service.backend
        assert isinstance(backend, LocalEngineBackend)
        assert isinstance(backend.embedder, EmbeddingClient)
        opener = backend.embedder._opener
        assert isinstance(opener, _FakeEmbeddingOpener)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request()).encode("utf-8")

        first_status, first_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(first_status, 200)
        self.assertTrue(first_payload["candidates"])
        self.assertEqual(opener.calls, 1)

        backend.store.add_problem(
            source="unit-test",
            external_id="missing-vector",
            title="缺少向量的合成候选",
            statement="合成测试文本",
            content_hash="missing-vector-hash",
        )
        with (
            patch("anklang.server.evaluate") as evaluate_mock,
            patch.object(backend, "search", wraps=backend.search) as search_mock,
        ):
            for _ in range(2):
                status, payload = harness.request(
                    "POST", "/api/v1/checks/similarity", body, headers
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["candidates"], [])
                self.assertFalse(payload["recommendation"]["blockSubmission"])
                self.assertEqual(
                    payload["recommendation"]["message"],
                    "本次未能完成原题检索，请稍后重试并由审题人手工核对。",
                )
        evaluate_mock.assert_not_called()
        self.assertEqual(search_mock.call_count, 2)
        self.assertEqual(opener.calls, 1)
        self.assertEqual(
            backend.describe_health()["vectorIndexStatus"],
            "incomplete_vectors",
        )

    def test_cache_is_bound_to_corpus_revision(self) -> None:
        service = self._service()
        backend = service.backend
        assert isinstance(backend, LocalEngineBackend)
        assert isinstance(backend.embedder, EmbeddingClient)
        opener = backend.embedder._opener
        assert isinstance(opener, _FakeEmbeddingOpener)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request()).encode("utf-8")

        first_status, first_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(len(first_payload["candidates"]), 1)
        self.assertEqual(opener.calls, 1)

        backend.store.add_problem(
            source="unit-test",
            external_id="new-complete-vector",
            title="新加入的合成候选",
            statement="给定 n 个整数，输出它们的和。",
            content_hash="new-complete-vector-hash",
            embedding=[1.0, 0.0],
            index_spec=_INDEX_SPEC,
        )
        second_status, second_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_payload["candidates"]), 2)
        self.assertEqual(opener.calls, 2)

        third_status, third_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(third_status, 200)
        self.assertEqual(third_payload, second_payload)
        self.assertEqual(opener.calls, 2)

    def test_same_connection_title_and_url_update_advances_cache_identity(
        self,
    ) -> None:
        service = self._service()
        backend = service.backend
        assert isinstance(backend, LocalEngineBackend)
        assert isinstance(backend.embedder, EmbeddingClient)
        opener = backend.embedder._opener
        assert isinstance(opener, _FakeEmbeddingOpener)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request()).encode("utf-8")

        first_status, first_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(first_payload["candidates"][0]["title"], "数组求和")
        self.assertEqual(opener.calls, 1)

        self.assertEqual(
            backend.store.add_problem(
                source="unit-test",
                external_id="array-sum",
                title="更新后的合成标题",
                url="https://example.invalid/updated-candidate",
                statement="给定 n 个整数，输出它们的和。",
                content_hash="h1",
                source_updated_at="2026-01-02T00:00:00.000Z",
            ),
            "updated",
        )
        second_status, second_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(
            second_payload["candidates"][0]["title"],
            "更新后的合成标题",
        )
        self.assertEqual(
            second_payload["candidates"][0]["url"],
            "https://example.invalid/updated-candidate",
        )
        self.assertEqual(opener.calls, 2)

        third_status, third_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(third_status, 200)
        self.assertEqual(third_payload, second_payload)
        self.assertEqual(opener.calls, 2)

    def test_cached_result_is_rejected_after_external_metadata_damage(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="anklang-cache-gate-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "index.db"
        service = self._service(str(path))
        backend = service.backend
        assert isinstance(backend, LocalEngineBackend)
        assert isinstance(backend.embedder, EmbeddingClient)
        opener = backend.embedder._opener
        assert isinstance(opener, _FakeEmbeddingOpener)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request()).encode("utf-8")

        first_status, first_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(first_status, 200)
        self.assertTrue(first_payload["candidates"])
        self.assertEqual(opener.calls, 1)

        connection = sqlite3.connect(path)
        try:
            connection.execute("DELETE FROM index_metadata")
            connection.commit()
        finally:
            connection.close()

        with (
            patch("anklang.server.evaluate") as evaluate_mock,
            patch.object(backend, "search", wraps=backend.search) as search_mock,
        ):
            for _ in range(2):
                status, payload = harness.request(
                    "POST", "/api/v1/checks/similarity", body, headers
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["candidates"], [])
                self.assertEqual(
                    payload["recommendation"]["message"],
                    "本次未能完成原题检索，请稍后重试并由审题人手工核对。",
                )
        evaluate_mock.assert_not_called()
        self.assertEqual(search_mock.call_count, 2)
        self.assertEqual(opener.calls, 1)
        self.assertFalse(backend.describe_health()["indexMetadataReady"])

    def test_external_vector_change_advances_cache_identity_after_revalidation(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="anklang-cache-version-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "index.db"
        service = self._service(str(path))
        backend = service.backend
        assert isinstance(backend, LocalEngineBackend)
        assert isinstance(backend.embedder, EmbeddingClient)
        opener = backend.embedder._opener
        assert isinstance(opener, _FakeEmbeddingOpener)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        original_body = json.dumps(_request()).encode("utf-8")

        first_status, _ = harness.request(
            "POST", "/api/v1/checks/similarity", original_body, headers
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(opener.calls, 1)

        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE problems SET embedding = ? WHERE external_id = ?",
                (pack_embedding([0.0, 1.0]), "array-sum"),
            )
            connection.commit()
        finally:
            connection.close()

        revalidation_body = json.dumps(
            _request(content_hash="e" * 64)
        ).encode("utf-8")
        revalidation_status, _ = harness.request(
            "POST", "/api/v1/checks/similarity", revalidation_body, headers
        )
        self.assertEqual(revalidation_status, 200)
        self.assertEqual(opener.calls, 2)

        changed_status, changed_payload = harness.request(
            "POST", "/api/v1/checks/similarity", original_body, headers
        )
        self.assertEqual(changed_status, 200)
        self.assertEqual(opener.calls, 3)

        repeated_status, repeated_payload = harness.request(
            "POST", "/api/v1/checks/similarity", original_body, headers
        )
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated_payload, changed_payload)
        self.assertEqual(opener.calls, 3)

    def test_embedding_failure_returns_candidates_without_caching(self) -> None:
        submitted_statement = "数组 求和 投题题面不可泄露标记"
        candidate_excerpt = "数组 求和 候选正文不可泄露标记"
        external_error = "文字转数字服务错误不可泄露标记"
        config = _config(backend="local_engine", local_db_path=":memory:")
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        store.prepare_embedding_writes(_INDEX_SPEC)
        store.add_problem(
            source="unit-test",
            external_id="keyword-fallback",
            title="公开候选标题",
            statement=candidate_excerpt,
            content_hash="keyword-fallback-hash",
            embedding=[1.0, 0.0],
            index_spec=_INDEX_SPEC,
        )
        opener = _FailingEmbeddingOpener(external_error)
        embedder = EmbeddingClient(
            base_url="https://dashscope.test/compatible-mode/v1",
            api_key="test-key",
            model="text-embedding-v4",
            dimensions=2,
            opener=opener,
        )
        backend = LocalEngineBackend(
            store,
            embedder,
            vector_top_k=10,
            keyword_top_k=10,
        )
        service = AnklangService(
            config,
            backend,
            ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
            None,
        )
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request(statement=submitted_statement)).encode("utf-8")
        responses: list[dict[str, Any]] = []
        stderr = io.StringIO()

        with (
            contextlib.redirect_stderr(stderr),
            patch("anklang.server.evaluate") as evaluate_mock,
        ):
            for _ in range(2):
                status, payload = harness.request(
                    "POST",
                    "/api/v1/checks/similarity",
                    body,
                    headers,
                )
                self.assertEqual(status, 200)
                self.assertFalse(payload["recommendation"]["blockSubmission"])
                self.assertEqual(payload["candidates"], [])
                self.assertEqual(
                    payload["recommendation"]["message"],
                    "本次未能完成原题检索，请稍后重试并由审题人手工核对。",
                )
                responses.append(payload)

        self.assertEqual(opener.calls, 2)
        evaluate_mock.assert_not_called()
        serialized = json.dumps(responses, ensure_ascii=False)
        captured_stderr = stderr.getvalue()
        for secret in (submitted_statement, candidate_excerpt, external_error):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, captured_stderr)
        self.assertNotIn("degraded", serialized)
        self.assertNotIn("review_failed", serialized)

    def test_health_reports_local_engine_info(self) -> None:
        service = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        status, payload = harness.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["backend"], "local_engine")
        self.assertEqual(payload["localProblemCount"], 1)
        self.assertTrue(payload["embeddingAvailable"])
        self.assertTrue(payload["indexMetadataReady"])
        self.assertTrue(payload["vectorIndexReady"])
        self.assertEqual(payload["status"], "ok")

    def test_no_embedder_legacy_vectors_return_fixed_incomplete_result(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="anklang-legacy-index-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "legacy.db"
        spec = EmbeddingIndexSpec("old-model", 2)
        builder = ProblemStore(str(path))
        builder.prepare_embedding_writes(spec)
        builder.add_problem(
            source="unit-test",
            external_id="legacy",
            title="旧索引候选",
            statement="数组 求和 旧索引候选",
            content_hash="legacy-hash",
            embedding=[1.0, 0.0],
            index_spec=spec,
        )
        builder.close()
        connection = sqlite3.connect(path)
        connection.execute("DELETE FROM index_metadata")
        connection.commit()
        connection.close()

        store = ProblemStore(str(path))
        self.addCleanup(store.close)
        config = _config(backend="local_engine", local_db_path=str(path))
        backend = LocalEngineBackend(store, embedder=None)
        service = AnklangService(
            config,
            backend,
            ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
            None,
        )
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request(statement="数组 求和")).encode("utf-8")

        with patch("anklang.server.evaluate") as evaluate_mock:
            for _ in range(2):
                status, payload = harness.request(
                    "POST",
                    "/api/v1/checks/similarity",
                    body,
                    headers,
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["candidates"], [])
                self.assertFalse(payload["recommendation"]["blockSubmission"])
                self.assertEqual(
                    payload["recommendation"]["message"],
                    "本次未能完成原题检索，请稍后重试并由审题人手工核对。",
                )
        evaluate_mock.assert_not_called()
        self.assertIsNone(service.cache.get("d" * 64))

        status, health = harness.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "degraded")
        self.assertFalse(health["indexMetadataReady"])
        self.assertEqual(health["vectorIndexStatus"], "legacy_vectors")


if __name__ == "__main__":
    unittest.main()
