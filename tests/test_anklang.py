"""Anklang 阶段 1 测试。仅用标准库 unittest，不做真实网络调用。

运行：python -m unittest discover -s tests
"""
from __future__ import annotations

import io
import json
import re
import unittest
from typing import Any

from anklang.backends.reverse_proxy import ReverseProxyBackend
from anklang.cache import ResultCache
from anklang.config import AppConfig
from anklang.contracts import ContractError, build_result, parse_request
from anklang.review import evaluate
from anklang.server import AnklangService, make_handler
from anklang.yuantiji import YuantijiClient


def _config(**overrides: Any) -> AppConfig:
    base = dict(
        port=8730,
        service_token="service-token-abcdef123456",
        yuantiji_base_url="https://yuantiji.test",
        yuantiji_timeout_seconds=30.0,
        yuantiji_minimum_interval_seconds=0.0,
        search_k=8,
        use_rerank=False,
        minimum_similarity=0.5,
        block_threshold=0.93,
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


def _request(content_hash: str | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "1",
        "requestId": "11111111-1111-1111-1111-111111111111",
        "contentHash": content_hash or ("a" * 64),
        "problem": {
            "title": "数组求和",
            "type": "traditional",
            "tagIds": ["math.basic"],
            "basicStatement": "给定 n 个整数，输出它们的和。",
        },
    }


class FakeOpener:
    """模拟 urllib.request.urlopen：按 URL 返回预置响应。"""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def __call__(self, request: Any, timeout: float) -> Any:  # noqa: ARG002
        url = request.full_url
        self.calls.append(url)
        payload = self._responses.get(url, {})
        body = json.dumps(payload).encode("utf-8")

        class _Response:
            def __enter__(self_inner) -> "_Response":
                return self_inner

            def __exit__(self_inner, *_args: Any) -> None:
                return None

            def read(self_inner, _limit: int) -> bytes:
                return body

        return _Response()


class ContractTests(unittest.TestCase):
    def test_parse_rejects_wrong_api_version(self) -> None:
        bad = _request()
        bad["apiVersion"] = "2"
        with self.assertRaises(ContractError):
            parse_request(bad)

    def test_parse_rejects_bad_hash(self) -> None:
        bad = _request()
        bad["contentHash"] = "not-a-hash"
        with self.assertRaises(ContractError):
            parse_request(bad)

    def test_build_result_echoes_hash_and_shapes_fields(self) -> None:
        result = build_result(
            content_hash="b" * 64,
            candidates=[
                {
                    "source": "EOlymp",
                    "externalId": "8763",
                    "title": "Sum of array",
                    "url": "https://eolymp.com/x",
                    "similarity": 0.9,
                    "sameProblemSuggestion": True,
                    "explanation": "几乎一致",
                }
            ],
            block_submission=True,
            message="发现相似题",
        )
        self.assertEqual(result["apiVersion"], "1")
        self.assertEqual(result["contentHash"], "b" * 64)
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}T.*Z$", result["checkedAt"]))
        self.assertTrue(result["recommendation"]["blockSubmission"])
        self.assertEqual(result["candidates"][0]["similarity"], 0.9)

    def test_build_result_rejects_out_of_range_similarity(self) -> None:
        with self.assertRaises(ContractError):
            build_result("c" * 64, [{"source": "s", "externalId": "e", "title": "t", "similarity": 1.5}], False, "x")


class MappingTests(unittest.TestCase):
    def test_prefers_rerank_over_cosine_and_clamps(self) -> None:
        mapped = YuantijiClient._map_candidate(
            {"uid": "X/1", "title": "T", "src": "X", "url": "https://x", "cos": 0.7, "rr": 1.3, "original": "abc"}
        )
        self.assertEqual(mapped["similarity"], 1.0)
        self.assertEqual(mapped["externalId"], "X/1")

    def test_falls_back_to_cosine_when_no_rerank(self) -> None:
        mapped = YuantijiClient._map_candidate({"uid": "Y/2", "title": "T", "cos": 0.42, "rr": None})
        self.assertAlmostEqual(mapped["similarity"], 0.42)


class ReviewTests(unittest.TestCase):
    def test_filters_below_minimum_and_blocks_above_threshold(self) -> None:
        config = _config(minimum_similarity=0.5, block_threshold=0.9)
        candidates = [
            {"source": "a", "externalId": "1", "title": "high", "similarity": 0.95},
            {"source": "b", "externalId": "2", "title": "mid", "similarity": 0.6},
            {"source": "c", "externalId": "3", "title": "low", "similarity": 0.3},
        ]
        decision = evaluate(config, {"title": "t", "basic_statement": "s"}, candidates, None)
        self.assertEqual(len(decision["candidates"]), 2)
        self.assertTrue(decision["block_submission"])
        self.assertEqual(decision["candidates"][0]["similarity"], 0.95)

    def test_no_block_when_all_below_threshold(self) -> None:
        config = _config(minimum_similarity=0.5, block_threshold=0.9)
        candidates = [{"source": "a", "externalId": "1", "title": "mid", "similarity": 0.6}]
        decision = evaluate(config, {"title": "t", "basic_statement": "s"}, candidates, None)
        self.assertFalse(decision["block_submission"])


class CacheTests(unittest.TestCase):
    def test_expires_and_evicts(self) -> None:
        clock = {"now": 0.0}
        cache = ResultCache(ttl_seconds=10, max_entries=2, clock=lambda: clock["now"])
        cache.set("h1", {"v": 1})
        self.assertEqual(cache.get("h1"), {"v": 1})
        clock["now"] = 11.0
        self.assertIsNone(cache.get("h1"))
        clock["now"] = 11.0
        cache.set("a", {"v": "a"})
        cache.set("b", {"v": "b"})
        cache.set("c", {"v": "c"})
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("c"))


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


class ServerTests(unittest.TestCase):
    def _service(self, **config_overrides: Any) -> tuple[AnklangService, FakeOpener]:
        config = _config(**config_overrides)
        opener = FakeOpener(
            {
                "https://yuantiji.test/api/search": {
                    "results": [
                        {"uid": "EOlymp/8763", "title": "Sum of array", "src": "EOlymp", "url": "https://x", "cos": 0.95, "rr": None},
                    ]
                },
                "https://yuantiji.test/api/health": {"ok": True, "problems": 254940},
            }
        )
        yuantiji = YuantijiClient("https://yuantiji.test", 30.0, 0.0, opener=opener)
        backend = ReverseProxyBackend(yuantiji, use_rerank=config.use_rerank)
        cache = ResultCache(config.cache_ttl_seconds, config.cache_max_entries)
        return AnklangService(config, backend, cache, None), opener

    def test_missing_token_is_rejected(self) -> None:
        service, _ = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        status, _ = harness.request("POST", "/api/v1/checks/similarity", json.dumps(_request()).encode("utf-8"))
        self.assertEqual(status, 401)

    def test_similarity_flow_and_cache(self) -> None:
        service, opener = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {"Authorization": "Bearer service-token-abcdef123456", "Content-Type": "application/json"}
        body = json.dumps(_request()).encode("utf-8")

        status, payload = harness.request("POST", "/api/v1/checks/similarity", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["contentHash"], "a" * 64)
        self.assertEqual(payload["apiVersion"], "1")
        self.assertTrue(payload["recommendation"]["blockSubmission"])
        self.assertEqual(payload["candidates"][0]["externalId"], "EOlymp/8763")
        search_calls = [call for call in opener.calls if call.endswith("/api/search")]
        self.assertEqual(len(search_calls), 1)

        # 第二次相同摘要走缓存，不再打上游。
        status2, payload2 = harness.request("POST", "/api/v1/checks/similarity", body, headers)
        self.assertEqual(status2, 200)
        self.assertEqual(payload2["contentHash"], "a" * 64)
        search_calls_after = [call for call in opener.calls if call.endswith("/api/search")]
        self.assertEqual(len(search_calls_after), 1)

    def test_health_reports_upstream(self) -> None:
        service, _ = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        status, payload = harness.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "anklang")
        self.assertEqual(payload["upstreamProblemCount"], 254940)

    def test_invalid_body_is_bad_request(self) -> None:
        service, _ = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {"Authorization": "Bearer service-token-abcdef123456"}
        status, _ = harness.request("POST", "/api/v1/checks/similarity", b"not json", headers)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
