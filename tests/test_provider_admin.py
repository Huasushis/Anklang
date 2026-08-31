"""embedding 提供方管理接口契约测试。

覆盖：鉴权（GET/PUT/DELETE 一律要求服务令牌）、设置/读取/更新/幂等/清除、
未配置提供方时查询与入库明确不可用、环境变量永不激活提供方、密钥永不回显、
clear 等待在途 embedding 结束后才返回。全部使用合成题面与回环连接。
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch
from typing import Any

from anklang.config import AppConfig, load_config
from anklang.http_api import AnklangHTTPServer, AnklangService, make_handler
from anklang.provider import ProviderConfig, ProviderRegistry
from anklang.store import ProblemStore
from ui.server import UpstreamSearchBackend, build_backend

_TOKEN = "synthetic-provider-token-123456"
_ADMIN_PATH = "/api/v1/admin/embedding-provider"
_UPSERT_PATH = "/api/v1/index/problems"
_QUERY_PATH = "/api/v2/checks/similarity"


def _config(**overrides: Any) -> AppConfig:
    values: dict[str, Any] = {
        "port": 8730,
        "service_token": _TOKEN,
        "search_k": 5,
        "minimum_similarity": 0.0,
        "local_db_path": ":memory:",
        "max_in_flight_checks": 8,
    }
    values.update(overrides)
    return AppConfig(**values)


def _provider_payload(**overrides: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "baseUrl": "https://provider.example.invalid/compatible-mode/v1",
        "apiKey": "synthetic-provider-api-key-42",
        "model": "replacement-model",
        "dimension": 2,
    }
    payload.update(overrides)
    return payload


def _query_payload() -> dict[str, object]:
    return {
        "apiVersion": "2",
        "requestId": "12345678-1234-4234-8234-123456789abd",
        "contentHash": "a" * 64,
        "problem": {
            "title": "provider query",
            "type": "traditional",
            "tagIds": ["synthetic"],
            "basicStatement": "provider query statement",
        },
    }


def _upsert_payload() -> dict[str, object]:
    return {
        "apiVersion": "1",
        "requestId": "12345678-1234-4234-8234-123456789abe",
        "externalId": "provider-problem-a",
        "updatedAt": "2026-08-28T00:00:00.000Z",
        "problem": {
            "title": "provider problem",
            "basicStatement": "provider problem statement",
        },
    }


class _MockEmbedder:
    model = "replacement-model"
    dimensions = 2

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0]


class _BlockingEmbedder(_MockEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        self.entered.set()
        self.release.wait(3)
        return [1.0, 0.0]


class _Harness:
    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        backend: UpstreamSearchBackend | None = None,
    ) -> None:
        self.store = ProblemStore(":memory:")
        self.backend = backend or UpstreamSearchBackend(
            self.store, ProviderRegistry()
        )
        self.service = AnklangService(config or _config(), self.backend)
        self.server = AnklangHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service)
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        token: str | None = _TOKEN,
        content_type: str = "application/json",
    ) -> tuple[int, bytes]:
        body: bytes | None = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = content_type
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, raw
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.backend.close()


class ProviderAdminAuthTests(unittest.TestCase):
    def test_get_put_delete_all_require_valid_service_token(self) -> None:
        for method in ("GET", "PUT", "DELETE"):
            with self.subTest(method=method):
                harness = _Harness()
                self.addCleanup(harness.close)
                payload = _provider_payload() if method == "PUT" else None
                for token in (None, "wrong-token"):
                    with self.subTest(method=method, token=token):
                        status, _ = harness.request(
                            method, _ADMIN_PATH, payload, token=token
                        )
                        self.assertEqual(status, 401)

    def test_admin_fails_closed_when_no_service_token_configured(self) -> None:
        harness = _Harness(config=_config(service_token=None))
        self.addCleanup(harness.close)
        status, _ = harness.request(_PUT := "PUT", _ADMIN_PATH, _provider_payload())
        self.assertEqual(status, 401)
        status, _ = harness.request("GET", _ADMIN_PATH)
        self.assertEqual(status, 401)


class ProviderAdminLifecycleTests(unittest.TestCase):
    def test_set_get_update_idempotent_and_clear(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)

        status, raw = harness.request("GET", _ADMIN_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"configured": False})

        payload = _provider_payload()
        status, raw = harness.request("PUT", _ADMIN_PATH, payload)
        self.assertEqual(status, 200)
        expected = {
            "configured": True,
            "baseUrl": str(payload["baseUrl"]),
            "model": str(payload["model"]),
            "dimension": 2,
        }
        self.assertEqual(json.loads(raw), expected)

        status, raw = harness.request("GET", _ADMIN_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), expected)

        updated = _provider_payload(
            baseUrl="https://provider-2.example.invalid/compatible-mode/v1",
            model="replacement-model-v2",
            dimension=4,
        )
        status, raw = harness.request("PUT", _ADMIN_PATH, updated)
        self.assertEqual(status, 200)
        updated_expected = {
            "configured": True,
            "baseUrl": str(updated["baseUrl"]),
            "model": "replacement-model-v2",
            "dimension": 4,
        }
        self.assertEqual(json.loads(raw), updated_expected)

        status, raw = harness.request("PUT", _ADMIN_PATH, updated)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), updated_expected)

        status, raw = harness.request("DELETE", _ADMIN_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"configured": False})

        status, raw = harness.request("GET", _ADMIN_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"configured": False})

    def test_put_rejects_invalid_bodies_and_content_type(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        bad_payloads: list[dict[str, object]] = [
            _provider_payload(extra=True),
            {"baseUrl": "https://x.invalid", "model": "m", "dimension": 2},
            _provider_payload(baseUrl="ftp://x.invalid"),
            _provider_payload(baseUrl="http://public.example.invalid/v1"),
            _provider_payload(baseUrl="https://x.invalid/v1?tenant=unsafe"),
            _provider_payload(protocol="other"),
            _provider_payload(apiKey=""),
            _provider_payload(dimension=0),
            _provider_payload(dimension=True),
            _provider_payload(model=""),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                status, _ = harness.request("PUT", _ADMIN_PATH, payload)
                self.assertEqual(status, 400)
        status, _ = harness.request(
            "PUT",
            _ADMIN_PATH,
            _provider_payload(),
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        status, _ = harness.request("POST", _ADMIN_PATH, _provider_payload())
        self.assertEqual(status, 405)

    def test_openai_protocol_and_trailing_slash_are_normalized(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)

        status, raw = harness.request(
            "PUT",
            _ADMIN_PATH,
            _provider_payload(
                protocol="openai",
                baseUrl="https://provider.example.invalid/compatible-mode/v1/",
            ),
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(raw),
            {
                "configured": True,
                "baseUrl": "https://provider.example.invalid/compatible-mode/v1",
                "model": "replacement-model",
                "dimension": 2,
            },
        )


class ProviderRedactionTests(unittest.TestCase):
    def test_api_key_never_appears_in_any_admin_response(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        secret = "synthetic-very-secret-key-9f4c"

        for method, payload in (("PUT", _provider_payload(apiKey=secret)), ):
            status, raw = harness.request(method, _ADMIN_PATH, payload)
            self.assertEqual(status, 200)
            self.assertNotIn(secret.encode("utf-8"), raw)
            self.assertNotIn(b"apiKey", raw)

        status, raw = harness.request("GET", _ADMIN_PATH)
        self.assertEqual(status, 200)
        self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertNotIn(b"apiKey", raw)

        status, raw = harness.request("DELETE", _ADMIN_PATH)
        self.assertEqual(status, 200)
        self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertNotIn(b"apiKey", raw)


class ProviderEnvironmentSafetyTests(unittest.TestCase):
    _ENV = {
        "ANKLANG_SERVICE_TOKEN": _TOKEN,
        "DASHSCOPE_BASE_URL": "https://dashscope.example.invalid/compatible-mode/v1",
        "DASHSCOPE_API_KEY": "synthetic-env-key",
        "DASHSCOPE_EMBEDDING_MODEL": "replacement-model",
        "DASHSCOPE_EMBEDDING_DIM": "2",
        "ANKLANG_SEARCH_MODE": "local",
    }

    def test_dashscope_env_never_activates_provider(self) -> None:
        with patch.dict(os.environ, self._ENV, clear=True):
            config = load_config()
            backend = build_backend(config)
            self.addCleanup(backend.close)
            self.assertFalse(backend.provider.status()[0])
            self.assertFalse(backend.describe_health()["embeddingAvailable"])
            harness = _Harness(config=config, backend=backend)
            self.addCleanup(harness.close)
            status, raw = harness.request("GET", _ADMIN_PATH)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw), {"configured": False})
            status, raw = harness.request("POST", _QUERY_PATH, _query_payload())
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw)["completion"]["status"], "unavailable"
            )
            status, _ = harness.request("PUT", _UPSERT_PATH, _upsert_payload())
            self.assertEqual(status, 503)

    def test_restart_starts_unconfigured_even_with_env_present(self) -> None:
        with patch.dict(os.environ, self._ENV, clear=True):
            first = build_backend(load_config())
            second = build_backend(load_config())
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertFalse(first.provider.status()[0])
        self.assertFalse(second.provider.status()[0])
        first.provider.configure(
            ProviderConfig(
                base_url="https://provider.example.invalid/compatible-mode/v1",
                api_key="synthetic-instance-key",
                model="replacement-model",
                dimension=2,
            )
        )
        self.assertTrue(first.provider.status()[0])
        self.assertFalse(second.provider.status()[0])


class ProviderThreadSafetyTests(unittest.TestCase):
    def test_clear_waits_for_in_flight_embedding(self) -> None:
        embedder = _BlockingEmbedder()
        registry = ProviderRegistry(initial=embedder)
        started = threading.Event()
        finished = threading.Event()

        def use() -> None:
            client = registry.acquire()
            started.set()
            try:
                client.embed_one("blocking statement")
            finally:
                registry.release()
            finished.set()

        worker = threading.Thread(target=use)
        worker.start()
        self.assertTrue(started.wait(2))
        cleared = threading.Event()

        def do_clear() -> None:
            registry.clear()
            cleared.set()

        clearer = threading.Thread(target=do_clear)
        clearer.start()
        time.sleep(0.2)
        self.assertFalse(cleared.is_set())
        embedder.release.set()
        worker.join(2)
        clearer.join(2)
        self.assertTrue(cleared.is_set())
        self.assertEqual(registry.in_flight, 0)
        self.assertIsNone(registry.acquire())


class ProviderSyntheticEmbeddingTests(unittest.TestCase):
    def test_upsert_query_hit_then_clear_makes_both_unavailable(self) -> None:
        backend = UpstreamSearchBackend(
            ProblemStore(":memory:"), ProviderRegistry(initial=_MockEmbedder())
        )
        harness = _Harness(backend=backend)
        self.addCleanup(harness.close)

        status, raw = harness.request("PUT", _UPSERT_PATH, _upsert_payload())
        self.assertEqual(status, 200)

        status, raw = harness.request("POST", _QUERY_PATH, _query_payload())
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(body["completion"]["status"], "complete")
        self.assertEqual(
            body["candidates"][0]["externalId"], "provider-problem-a"
        )

        status, raw = harness.request("DELETE", _ADMIN_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"configured": False})

        status, raw = harness.request("POST", _QUERY_PATH, _query_payload())
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(raw)["completion"]["status"], "unavailable"
        )
        status, _ = harness.request("PUT", _UPSERT_PATH, _upsert_payload())
        self.assertEqual(status, 503)


if __name__ == "__main__":
    unittest.main()
