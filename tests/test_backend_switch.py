"""验证本地检索后端（local_engine）的构建和 HTTP 服务链路。

1. build_backend() 构造 LocalEngineBackend；没配置百炼凭据时优雅降级为
   embedder=None，不报错。
2. 选中 local_engine 后端时，服务器整条 HTTP 链路（鉴权、契约校验、健康检查）能
   跑通——用注入的假 embedding 和预先写入的题库，不做真实网络调用。
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
        search_k=8,
        minimum_similarity=0.1,
        backend="local_engine",
        local_db_path=":memory:",
    )
    base.update(overrides)
    return AppConfig(**base)


def _request(
    content_hash: str | None = None,
    statement: str = "给定 n 个整数，输出它们的和。",
) -> dict[str, Any]:
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
    def test_default_config_selects_local_engine(self) -> None:
        config = _config(local_db_path=":memory:")
        backend = build_backend(config)
        self.assertIsInstance(backend, LocalEngineBackend)
        self.assertIsNone(backend.embedder)
        health = backend.describe_health()
        self.assertEqual(health["vectorIndexStatus"], "empty_corpus")
        self.assertFalse(health["indexMetadataReady"])
        backend.store.close()

    def test_local_engine_with_dashscope_credentials_builds_embedder(self) -> None:
        config = _config(
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
                overrides: dict[str, Any] = {"local_db_path": ":memory:"}
                if embedding_enabled:
                    overrides.update(
                        {
                            "dashscope_base_url": "https://dashscope.test/compatible-mode/v1",
                            "dashscope_api_key": "test-key",
                            "dashscope_embedding_model": "text-embedding-v4",
                            "dashscope_embedding_dim": 2,
                        }
                    )
                config = _config(**overrides)
                backend = build_backend(config)
                assert isinstance(backend, LocalEngineBackend)
                try:
                    service = AnklangService(config, backend)
                    harness = _ServerHarness(service)
                    try:
                        headers = {
                            "Authorization": "Bearer service-token-abcdef123456",
                            "Content-Type": "application/json",
                        }
                        body = json.dumps(_request()).encode("utf-8")
                        status, payload = harness.request(
                            "POST",
                            "/api/v1/checks/similarity",
                            body,
                            headers,
                        )
                        self.assertEqual(status, 503)
                        self.assertEqual(payload["error"]["code"], "CHECK_INCOMPLETE")
                        health_status, health = harness.request("GET", "/api/v1/health")
                        self.assertEqual(health_status, 200)
                        self.assertEqual(health["status"], "degraded")
                        self.assertEqual(health["vectorIndexStatus"], "empty_corpus")
                    finally:
                        harness.close()
                finally:
                    backend.store.close()

    def _service(self, db_path: str = ":memory:") -> AnklangService:
        config = _config(local_db_path=db_path)
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
        return AnklangService(config, backend)

    def test_similarity_flow_uses_local_engine(self) -> None:
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
        body = json.dumps(_request(statement="给定 n 个整数，输出它们的和。")).encode("utf-8")

        status, payload = harness.request("POST", "/api/v1/checks/similarity", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["apiVersion"], "1")
        self.assertEqual(payload["candidates"][0]["externalId"], "array-sum")
        self.assertEqual(payload["candidates"][0]["source"], "unit-test")

        # 每次请求都重新调用后端——没有缓存。
        second_status, second_payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_payload["contentHash"], payload["contentHash"])
        self.assertEqual(opener.calls, 2)

    def test_embedding_failure_returns_partial_without_caching(self) -> None:
        submitted_statement = "数组 求和 投题题面不可泄露标记"
        candidate_excerpt = "数组 求和 候选正文不可泄露标记"
        external_error = "文字转数字服务错误不可泄露标记"
        config = _config(local_db_path=":memory:")
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
        backend = LocalEngineBackend(store, embedder, vector_top_k=10, keyword_top_k=10)
        service = AnklangService(config, backend)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        request = _request(statement=submitted_statement)
        request["apiVersion"] = "2"
        body = json.dumps(request).encode("utf-8")
        responses: list[dict[str, Any]] = []
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            for _ in range(2):
                status, payload = harness.request(
                    "POST",
                    "/api/v2/checks/similarity",
                    body,
                    headers,
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["completion"]["status"], "partial")
                self.assertEqual(payload["completion"]["reasonCode"], "search_partial")
                self.assertEqual(payload["reuse"], {"policy": "no-store"})
                self.assertEqual(payload["candidates"][0]["externalId"], "keyword-fallback")
                responses.append(payload)

        self.assertEqual(opener.calls, 2)
        serialized = json.dumps(responses, ensure_ascii=False)
        captured_stderr = stderr.getvalue()
        for secret in (submitted_statement, candidate_excerpt, external_error):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, captured_stderr)

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
        config = _config(local_db_path=str(path))
        backend = LocalEngineBackend(store, embedder=None)
        service = AnklangService(config, backend)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request(statement="数组 求和")).encode("utf-8")

        for _ in range(2):
            status, payload = harness.request(
                "POST",
                "/api/v1/checks/similarity",
                body,
                headers,
            )
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "CHECK_INCOMPLETE")

        status, health = harness.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "degraded")
        self.assertFalse(health["indexMetadataReady"])
        self.assertEqual(health["vectorIndexStatus"], "legacy_vectors")

    def test_incomplete_vectors_returns_503(self) -> None:
        """题库中出现没有向量的新题目时，检索结果为不完整。"""
        service = self._service()
        backend = service.backend
        assert isinstance(backend, LocalEngineBackend)
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

        backend.store.add_problem(
            source="unit-test",
            external_id="missing-vector",
            title="缺少向量的合成候选",
            statement="合成测试文本",
            content_hash="missing-vector-hash",
        )
        status, payload = harness.request(
            "POST", "/api/v1/checks/similarity", body, headers
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "CHECK_INCOMPLETE")
        self.assertEqual(
            backend.describe_health()["vectorIndexStatus"],
            "incomplete_vectors",
        )


if __name__ == "__main__":
    unittest.main()
