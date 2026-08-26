"""Anklang 查询契约边界测试；补足既有 HTTP 契约测试未覆盖的边界行，全部使用合成数据。"""
from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any

from anklang.backends import BackendSearchResult
from anklang.config import AppConfig
from anklang.http_api import AnklangService, make_handler


def _config(**overrides: Any) -> AppConfig:
    values = dict(
        port=8730,
        service_token="service-token-abcdef123456",
        search_k=8,
        minimum_similarity=0.5,
    )
    values.update(overrides)
    return AppConfig(**values)


def _request(version: str) -> dict[str, Any]:
    return {
        "apiVersion": version,
        "requestId": "55555555-5555-5555-8555-555555555555",
        "contentHash": "5" * 64,
        "problem": {
            "title": "合成边界标题",
            "type": "traditional",
            "tagIds": ["math.basic"],
            "basicStatement": "合成边界题面 gamma delta",
        },
    }


def _candidate(similarity: float, *, url: str | None = None) -> dict[str, Any]:
    item = {
        "source": "synthetic",
        "externalId": f"synthetic/{similarity!r}",
        "title": "合成边界候选",
        "similarity": similarity,
    }
    if url is not None:
        item["url"] = url
    return item


class _ScriptedBackend:
    def __init__(self, outcomes: list[BackendSearchResult | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def search(self, _query_text: str, _k: int) -> BackendSearchResult:
        self.calls += 1
        outcome = self._outcomes[(self.calls - 1) % len(self._outcomes)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def describe_health(self) -> dict[str, Any]:
        return {"localStoreReady": True, "indexMetadataReady": True}


class _Harness:
    def __init__(self, service: AnklangService) -> None:
        handler_class = make_handler(service)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | dict[str, Any] = b"",
        *,
        authorized: bool = True,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        headers: dict[str, str] = {}
        if body:
            headers["Content-Type"] = "application/json"
        if authorized:
            headers["Authorization"] = "Bearer service-token-abcdef123456"
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body or None, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            resp_headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, payload, resp_headers
        finally:
            conn.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class QueryContractBoundaryTests(unittest.TestCase):
    def _harness(
        self,
        backend: _ScriptedBackend,
        **config_overrides: Any,
    ) -> tuple[_Harness, _ScriptedBackend]:
        service = AnklangService(_config(**config_overrides), backend)
        harness = _Harness(service)
        self.addCleanup(harness.close)
        return harness, backend

    def test_complete_with_empty_candidates_has_exact_shape(self) -> None:
        harness, backend = self._harness(_ScriptedBackend([BackendSearchResult([])]))
        status, payload, headers = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {"apiVersion", "contentHash", "checkedAt", "completion", "candidates"},
        )
        self.assertEqual(payload["completion"], {
            "status": "complete",
            "reasonCode": "complete",
            "retryable": False,
        })
        self.assertEqual(payload["candidates"], [])
        datetime.strptime(payload["checkedAt"], "%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(backend.calls, 1)

    def test_similarity_boundaries_zero_and_one_survive_contract(self) -> None:
        harness, _ = self._harness(
            _ScriptedBackend(
                [BackendSearchResult([_candidate(0.0), _candidate(1.0)])]
            ),
            minimum_similarity=0.0,
        )
        status, payload, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["completion"]["status"], "complete")
        marker = "不应出站的合成候选标记"
        harness, _ = self._harness(
            _ScriptedBackend(
                [BackendSearchResult([_candidate(float("inf"), url="https://x.invalid/a")])]
            )
        )
        harness.request("POST", "/api/v2/checks/similarity", _request("2"))
        # 直接构造带标记的候选重新请求，验证失败路径不泄漏原始候选。
        backend = _ScriptedBackend(
            [
                BackendSearchResult(
                    [{"source": "synthetic", "externalId": marker, "title": marker, "similarity": float("inf")}]
                )
            ]
        )
        service = AnklangService(_config(), backend)
        leak_harness = _Harness(service)
        self.addCleanup(leak_harness.close)
        status, payload, headers = leak_harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["completion"]["status"], "unavailable")
        self.assertEqual(payload["completion"]["reasonCode"], "service_invalid_response")
        self.assertFalse(payload["completion"]["retryable"])
        self.assertNotIn(marker, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_out_of_range_similarity_fails_closed(self) -> None:
        harness, _ = self._harness(
            _ScriptedBackend([BackendSearchResult([_candidate(1.000001)])])
        )
        status, payload, headers = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["completion"]["reasonCode"], "service_invalid_response")
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_candidates_return_strictly_descending_over_http(self) -> None:
        harness, _ = self._harness(
            _ScriptedBackend(
                [BackendSearchResult([_candidate(0.7), _candidate(0.99), _candidate(0.85)])]
            )
        )
        status, payload, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        similarities = [candidate["similarity"] for candidate in payload["candidates"]]
        self.assertEqual(similarities, sorted(similarities, reverse=True))
        self.assertEqual(similarities, [0.99, 0.85, 0.7])

    def test_optional_url_present_or_absent_without_extra_fields(self) -> None:
        harness, _ = self._harness(
            _ScriptedBackend(
                [
                    BackendSearchResult(
                        [
                            _candidate(0.9, url="https://example.invalid/p1"),
                            _candidate(0.8),
                        ]
                    )
                ]
            )
        )
        status, payload, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        first, second = payload["candidates"]
        self.assertEqual(set(first), {"source", "externalId", "title", "similarity", "url"})
        self.assertEqual(first["url"], "https://example.invalid/p1")
        self.assertEqual(set(second), {"source", "externalId", "title", "similarity"})

    def test_minimum_similarity_floor_is_inclusive_display_filter(self) -> None:
        harness, _ = self._harness(
            _ScriptedBackend(
                [BackendSearchResult([_candidate(0.59), _candidate(0.6)])]
            ),
            minimum_similarity=0.6,
        )
        status, payload, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["completion"]["status"], "complete")
        self.assertEqual(
            [candidate["similarity"] for candidate in payload["candidates"]],
            [0.6],
        )

    def test_version_body_cross_pairs_are_fixed_400_without_backend_calls(self) -> None:
        harness, backend = self._harness(_ScriptedBackend([]))
        cases = (
            ("/api/v2/checks/similarity", _request("1")),
            ("/api/v1/checks/similarity", _request("2")),
        )
        for path, body in cases:
            with self.subTest(path=path):
                status, payload, headers = harness.request("POST", path, body)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)
                self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(backend.calls, 0)

    def test_backend_exception_maps_to_retryable_unavailable_without_text_leak(self) -> None:
        secret_marker = "合成内部异常文本"
        harness, backend = self._harness(
            _ScriptedBackend([RuntimeError(secret_marker)])
        )
        status, payload, headers = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["completion"]["status"], "unavailable")
        self.assertEqual(payload["completion"]["reasonCode"], "internal_error")
        self.assertTrue(payload["completion"]["retryable"])
        self.assertNotIn(secret_marker, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(headers.get("cache-control"), "no-store")
        v1_status, v1_payload, _ = harness.request(
            "POST", "/api/v1/checks/similarity", _request("1")
        )
        self.assertEqual(v1_status, 503)
        self.assertEqual(v1_payload["error"]["code"], "CHECK_INCOMPLETE")
        self.assertNotIn(secret_marker, json.dumps(v1_payload, ensure_ascii=False))
        self.assertEqual(backend.calls, 2)


if __name__ == "__main__":
    unittest.main()
