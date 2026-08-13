"""部署可观测性契约测试：修订响应头与提供方无关的就绪检查。

全部使用回环连接和合成正文；不发起任何外部网络请求，也不读取私有数据。
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from anklang.backends import BackendSearchResult
from anklang.cache import ResultCache
from anklang.config import AppConfig, ConfigError
from anklang.server import AnklangService, ServiceRuntime, make_handler


def _config(**overrides: Any) -> AppConfig:
    values = dict(
        port=8730,
        service_token="service-token-abcdef123456",
        yuantiji_base_url="https://yuantiji.test",
        yuantiji_timeout_seconds=12.0,
        yuantiji_minimum_interval_seconds=0.0,
        search_k=8,
        use_rerank=False,
        minimum_similarity=0.5,
        block_threshold=0.9,
        cache_ttl_seconds=3600,
        cache_max_entries=100,
        llm_review_enabled=False,
        llm_base_url=None,
        llm_api_key=None,
        llm_model="synthetic-review-model",
        llm_review_top_n=2,
        llm_timeout_seconds=10.0,
    )
    values.update(overrides)
    return AppConfig(**values)


class _NoCallBackend:
    """一个搜索后端桩：记录所有调用，永远不主动发起网络请求。"""

    def __init__(self) -> None:
        self.search_calls = 0
        self.health_calls = 0
        self.describe_health_calls = 0

    def search(self, _query_text: str, _k: int) -> BackendSearchResult:
        self.search_calls += 1
        return BackendSearchResult([])

    def describe_health(self) -> dict[str, Any]:
        self.describe_health_calls += 1
        return {"upstreamReady": True}


class _Harness:
    def __init__(
        self,
        backend: _NoCallBackend,
        config: AppConfig | None = None,
    ) -> None:
        self.config = config or _config()
        self.runtime = ServiceRuntime(self.config.max_in_flight_checks)
        self.service = AnklangService(
            self.config,
            backend,
            ResultCache(
                self.config.cache_ttl_seconds,
                self.config.cache_max_entries,
            ),
            None,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, self.runtime)
        )
        self.port = int(self.server.server_address[1])
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self._thread.start()

    def get(self, path: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            raw = response.read()
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
            return (
                response.status,
                decoded,
                {name.lower(): value for name, value in response.getheaders()},
            )
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RevisionHeaderTests(unittest.TestCase):
    """修订标识 X-Anklang-Revision 在成功和错误响应上都必须出现。"""

    def test_revision_header_on_live_success(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision="abc1234"))
        self.addCleanup(harness.close)
        status, payload, headers = harness.get("/api/v1/live")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-anklang-revision"], "abc1234")

    def test_revision_header_on_404_error(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision="v1.2.3-rc4"))
        self.addCleanup(harness.close)
        status, payload, headers = harness.get("/api/v1/nonexistent")
        self.assertEqual(status, 404)
        self.assertEqual(headers["x-anklang-revision"], "v1.2.3-rc4")

    def test_revision_header_on_ready_success(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision="deadbeef"))
        self.addCleanup(harness.close)
        status, payload, headers = harness.get("/api/v1/ready")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-anklang-revision"], "deadbeef")

    def test_no_revision_header_when_not_set(self) -> None:
        """revision 留空时不输出 X-Anklang-Revision 头（确定性缺失策略）。"""
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision=None))
        self.addCleanup(harness.close)
        status, _payload, headers = harness.get("/api/v1/live")
        self.assertEqual(status, 200)
        self.assertNotIn("x-anklang-revision", headers)

    def test_revision_header_consistent_across_responses(self) -> None:
        """成功和错误响应的修订标识必须一致。"""
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision="consist-rev"))
        self.addCleanup(harness.close)
        _s1, _p1, h1 = harness.get("/api/v1/live")
        _s2, _p2, h2 = harness.get("/api/v1/ready")
        _s3, _p3, h3 = harness.get("/api/v1/bad")
        self.assertEqual(h1["x-anklang-revision"], "consist-rev")
        self.assertEqual(h2["x-anklang-revision"], "consist-rev")
        self.assertEqual(h3["x-anklang-revision"], "consist-rev")


class RevisionValidationTests(unittest.TestCase):
    """ANKLANG_REVISION 的确定性格式校验。"""

    def test_valid_revision(self) -> None:
        with patch.dict(os.environ, {"ANKLANG_REVISION": "abc1234"}, clear=False):
            config = _config(revision="abc1234")
            self.assertEqual(config.revision, "abc1234")

    def test_empty_revision_means_none(self) -> None:
        with patch.dict(os.environ, {"ANKLANG_REVISION": ""}, clear=False):
            from anklang.config import _read_revision

            self.assertIsNone(_read_revision("ANKLANG_REVISION"))

    def test_invalid_revision_rejected(self) -> None:
        """包含空格、斜杠、特殊字符的修订标识被拒绝。"""
        from anklang.config import _REVISION_RE, _read_revision

        # 控制字符无法通过 os.environ 设置（OS 限制），直接验证正则不会匹配。
        self.assertIsNone(_REVISION_RE.fullmatch("abc\x00def"))

        for bad_value in (
            "abc 1234",
            "abc/1234",
            "abc;1234",
            "abc'1234",
            "abc\"1234",
            "../etc/passwd",
            "a" * 201,  # 超过 200 字符
        ):
            with self.subTest(bad_value=bad_value):
                with patch.dict(
                    os.environ, {"ANKLANG_REVISION": bad_value}, clear=False
                ):
                    with self.assertRaises(ConfigError):
                        _read_revision("ANKLANG_REVISION")

    def test_valid_characters_accepted(self) -> None:
        """字母、数字、点、下划线和连字符都被接受。"""
        from anklang.config import _read_revision

        for good_value in (
            "abc1234",
            "v1.2.3",
            "feature_branch",
            "rev-2026-08-13",
            "a" * 200,  # 恰好 200 字符
        ):
            with self.subTest(good_value=good_value):
                with patch.dict(
                    os.environ, {"ANKLANG_REVISION": good_value}, clear=False
                ):
                    result = _read_revision("ANKLANG_REVISION")
                    self.assertEqual(result, good_value)


class ReadinessEndpointTests(unittest.TestCase):
    """/api/v1/ready 是提供方无关的就绪检查。"""

    def test_ready_returns_200_when_accepting(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)
        status, payload, headers = harness.get("/api/v1/ready")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "anklang")
        self.assertEqual(payload["apiVersion"], "1")
        self.assertTrue(payload["ready"])

    def test_ready_returns_503_when_not_accepting(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)
        harness.runtime.begin_shutdown()
        status, payload, _headers = harness.get("/api/v1/ready")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["ready"])

    def test_ready_makes_zero_backend_or_health_calls(self) -> None:
        """就绪检查不调用后端搜索，也不调用 describe_health。"""
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)
        harness.get("/api/v1/ready")
        self.assertEqual(backend.search_calls, 0)
        self.assertEqual(backend.describe_health_calls, 0)

    def test_ready_returns_no_store_cache_control(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)
        _status, _payload, headers = harness.get("/api/v1/ready")
        self.assertEqual(headers["cache-control"], "no-store")

    def test_ready_distinct_from_live_and_health(self) -> None:
        """/api/v1/ready 只验证本地状态，不透传上游；/api/v1/health 调用 describe_health。"""
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)

        _s_ready, _p_ready, _h_ready = harness.get("/api/v1/ready")
        self.assertEqual(backend.describe_health_calls, 0)

        _s_live, _p_live, _h_live = harness.get("/api/v1/live")
        self.assertEqual(backend.describe_health_calls, 0)

        _s_health, _p_health, _h_health = harness.get("/api/v1/health")
        self.assertEqual(backend.describe_health_calls, 1)


class NoBackendTransportProofTests(unittest.TestCase):
    """证明 /api/v1/ready 不发起任何后端/网络传输。"""

    def test_ready_does_not_invoke_backend_search_method(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)

        # 多次调用 ready，确认后端搜索方法从未被调用
        for _ in range(3):
            harness.get("/api/v1/ready")

        self.assertEqual(backend.search_calls, 0)
        self.assertEqual(backend.describe_health_calls, 0)

    def test_ready_works_even_if_backend_would_raise(self) -> None:
        """即使后端的 search 方法会抛异常，ready 仍然成功返回。"""

        class _ExplodingBackend:
            def search(self, _q: str, _k: int) -> BackendSearchResult:
                raise RuntimeError("backend should not be called")

            def describe_health(self) -> dict[str, Any]:
                raise RuntimeError("health should not be called")

        backend = _ExplodingBackend()
        config = _config()
        runtime = ServiceRuntime(config.max_in_flight_checks)
        service = AnklangService(
            config,
            backend,  # type: ignore[arg-type]
            ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
            None,
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(service, runtime)
        )
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/api/v1/ready")
                response = connection.getresponse()
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["ready"])
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
