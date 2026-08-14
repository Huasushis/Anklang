"""部署可观测性契约测试：就绪检查端点不发起任何后端/网络调用。

全部使用回环连接和合成正文；不发起任何外部网络请求，也不读取私有数据。
"""
from __future__ import annotations

import json
import threading
import unittest
import yaml
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from anklang.backends import BackendSearchResult
from anklang.config import AppConfig, ConfigError
from anklang.http_api import AnklangService, ServiceRuntime, make_handler


def _config(**overrides: Any) -> AppConfig:
    values = dict(
        port=8730,
        service_token="service-token-abcdef123456",
        search_k=8,
        minimum_similarity=0.5,
    )
    values.update(overrides)
    return AppConfig(**values)


class _NoCallBackend:
    """一个搜索后端桩：记录所有调用，永远不主动发起网络请求。"""

    def __init__(self) -> None:
        self.search_calls = 0
        self.describe_health_calls = 0

    def search(self, _query_text: str, _k: int) -> BackendSearchResult:
        self.search_calls += 1
        return BackendSearchResult(candidates=[])

    def describe_health(self) -> dict[str, Any]:
        self.describe_health_calls += 1
        return {"localStoreReady": True, "indexMetadataReady": True}


class _Harness:
    def __init__(
        self,
        service: AnklangService,
        runtime: ServiceRuntime | None = None,
    ) -> None:
        handler_class = make_handler(service, runtime)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body or None, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            resp_headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, payload, resp_headers
        finally:
            conn.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ReadinessEndpointTests(unittest.TestCase):
    """/api/v1/ready 是提供方无关的就绪检查，不调用后端。"""

    def test_ready_returns_ok_when_accepting(self) -> None:
        config = _config()
        backend = _NoCallBackend()
        service = AnklangService(config, backend)
        runtime = ServiceRuntime(config.max_in_flight_checks)
        harness = _Harness(service, runtime)
        self.addCleanup(harness.close)

        status, payload, _ = harness.request("GET", "/api/v1/ready")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ready"])
        self.assertEqual(backend.describe_health_calls, 0)

    def test_not_ready_returns_503_when_shutting_down(self) -> None:
        config = _config()
        backend = _NoCallBackend()
        service = AnklangService(config, backend)
        runtime = ServiceRuntime(config.max_in_flight_checks)
        harness = _Harness(service, runtime)
        self.addCleanup(harness.close)

        runtime.begin_shutdown()
        status, payload, _ = harness.request("GET", "/api/v1/ready")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(backend.describe_health_calls, 0)


class NoBackendTransportProofTests(unittest.TestCase):
    """证明 /api/v1/ready 不发起任何后端/网络传输。"""

    def test_ready_does_not_call_backend_search(self) -> None:
        config = _config()
        backend = _NoCallBackend()
        service = AnklangService(config, backend)
        runtime = ServiceRuntime(config.max_in_flight_checks)
        harness = _Harness(service, runtime)
        self.addCleanup(harness.close)

        for _ in range(3):
            status, _, _ = harness.request("GET", "/api/v1/ready")
            self.assertEqual(status, 200)

        self.assertEqual(backend.search_calls, 0)
        self.assertEqual(backend.describe_health_calls, 0)


class CacheControlHeaderTests(unittest.TestCase):
    """所有响应都带 Cache-Control: no-store。"""

    def test_live_has_no_store_header(self) -> None:
        config = _config()
        backend = _NoCallBackend()
        service = AnklangService(config, backend)
        runtime = ServiceRuntime(config.max_in_flight_checks)
        harness = _Harness(service, runtime)
        self.addCleanup(harness.close)

        status, _, headers = harness.request("GET", "/api/v1/live")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_health_has_no_store_header(self) -> None:
        config = _config()
        backend = _NoCallBackend()
        service = AnklangService(config, backend)
        runtime = ServiceRuntime(config.max_in_flight_checks)
        harness = _Harness(service, runtime)
        self.addCleanup(harness.close)

        status, _, headers = harness.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")


class ComposeEnvFileTests(unittest.TestCase):
    """随附的 Compose 文件结构合理，不再注入修订标识。"""

    def test_compose_has_no_revision_build_arg(self) -> None:
        with open("compose.yaml", "r") as f:
            compose = yaml.safe_load(f)
        build_args = compose.get("services", {}).get("anklang", {}).get("build", {}).get("args", {})
        self.assertNotIn("ANKLANG_REVISION", build_args)

    def test_env_has_no_proxy_review_cache_or_policy_configuration(self) -> None:
        with open(".env.example", "r") as f:
            content = f.read()
        self.assertNotIn("YUANTIJI", content)
        self.assertNotIn("ANKLANG_LLM", content)
        self.assertNotIn("ANKLANG_CACHE", content)
        self.assertNotIn("ANKLANG_REVISION", content)
        self.assertNotIn("ANKLANG_USE_RERANK", content)

        self.assertNotIn("ANKLANG_BLOCK_THRESHOLD", content)
        self.assertNotIn("ANKLANG_SIMILARITY_BLOCK_ENABLED", content)

class ConfigHasNoRevisionTests(unittest.TestCase):
    """AppConfig 不再包含 revision 字段。"""

    def test_app_config_has_no_revision_field(self) -> None:
        config = _config()
        self.assertFalse(hasattr(config, "revision"))

    def test_load_config_ignores_revision_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"ANKLANG_REVISION": "should-be-ignored"},
            clear=True,
        ):
            config = load_config_safe()
        self.assertFalse(hasattr(config, "revision"))


def load_config_safe() -> AppConfig:
    """在最小环境上调用 load_config，不依赖测试框架。"""
    from anklang.config import load_config
    return load_config()


if __name__ == "__main__":
    unittest.main()
