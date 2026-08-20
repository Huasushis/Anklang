"""Anklang 核心测试。仅用标准库 unittest，不做真实网络调用。

运行：PYTHONPATH=. python3 -m unittest discover -s tests
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from typing import Any

from anklang.backends import BackendError, BackendSearchResult
from anklang.config import AppConfig
from anklang.contracts import (
    MAX_CANDIDATES,
    MAX_RESPONSE_BYTES,
    ContractError,
    build_result,
    build_v2_result,
    parse_request,
)
from anklang.http_api import AnklangService, _rank_candidates, make_handler


def _config(**overrides: Any) -> AppConfig:
    base = dict(
        port=8730,
        service_token="service-token-abcdef123456",
        search_k=8,
        minimum_similarity=0.5,
    )
    base.update(overrides)
    return AppConfig(**base)


def _request(content_hash: str | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "1",
        "requestId": "11111111-1111-4111-8111-111111111111",
        "contentHash": content_hash or ("a" * 64),
        "problem": {
            "title": "数组求和",
            "type": "traditional",
            "tagIds": ["math.basic"],
            "basicStatement": "给定 n 个整数，输出它们的和。",
        },
    }


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

    def test_parse_rejects_extra_fields_at_both_levels(self) -> None:
        extra_root = _request()
        extra_root["unexpected"] = True
        with self.assertRaises(ContractError):
            parse_request(extra_root)

        extra_problem = _request()
        extra_problem["problem"]["unexpected"] = True
        with self.assertRaises(ContractError):
            parse_request(extra_problem)

    def test_parse_requires_uuid_version_and_variant(self) -> None:
        for invalid in (
            "11111111111141118111111111111111",
            "11111111-1111-0111-8111-111111111111",
            "11111111-1111-4111-1111-111111111111",
            "普通文本",
        ):
            with self.subTest(invalid=invalid):
                bad = _request()
                bad["requestId"] = invalid
                with self.assertRaises(ContractError):
                    parse_request(bad)

    def test_parse_matches_javascript_string_length_for_emoji(self) -> None:
        valid = _request()
        valid["problem"]["title"] = "😀" * 100
        self.assertEqual(parse_request(valid)["title"], "😀" * 100)

        too_long = _request()
        too_long["problem"]["title"] = "😀" * 101
        with self.assertRaises(ContractError):
            parse_request(too_long)

    def test_parse_unhashable_problem_type_is_contract_error(self) -> None:
        bad = _request()
        bad["problem"]["type"] = []
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
        )
        self.assertEqual(result["apiVersion"], "1")
        self.assertEqual(result["contentHash"], "b" * 64)
        self.assertRegex(
            result["checkedAt"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        )
        self.assertEqual(result["candidates"][0]["similarity"], 0.9)
        self.assertEqual(
            set(result),
            {"apiVersion", "contentHash", "checkedAt", "candidates"},
        )
        self.assertEqual(
            set(result["candidates"][0]),
            {"source", "externalId", "title", "url", "similarity"},
        )

    def test_build_result_rejects_out_of_range_similarity(self) -> None:
        for value in (1.5, -0.1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ContractError):
                build_result(
                    "c" * 64,
                    [{"source": "s", "externalId": "e", "title": "t", "similarity": value}],
                )

    def test_build_result_rejects_empty_required_candidate_fields(self) -> None:
        for field in ("source", "externalId", "title"):
            candidate = {
                "source": "s",
                "externalId": "e",
                "title": "t",
                "similarity": 0.5,
            }
            candidate[field] = "   "
            with self.subTest(field=field), self.assertRaises(ContractError):
                build_result("d" * 64, [candidate])

    def test_build_result_omits_unsafe_url_and_internal_fields(self) -> None:
        for unsafe_url in (
            "https://user:password@example.invalid/x",
            "https://example.invalid:bad/x",
            "https://exa%mple.invalid/x",
            "http://256.256.256.256/",
            "https://",
            "https://example.invalid/has space",
            "https://example.invalid\\wrong",
        ):
            with self.subTest(unsafe_url=unsafe_url):
                result = build_result(
                    "e" * 64,
                    [
                        {
                            "source": "s",
                            "externalId": "e",
                            "title": "t",
                            "url": unsafe_url,
                            "similarity": 0.5,
                            "_reviewExcerpt": "不应进入响应的内部文字",
                        }
                    ],
                )
                candidate = result["candidates"][0]
                self.assertNotIn("url", candidate)
                self.assertNotIn("_reviewExcerpt", candidate)

    def test_build_result_treats_bom_as_trimmed_whitespace(self) -> None:
        with self.assertRaises(ContractError):
            build_result(
                "e" * 64,
                [
                    {
                        "source": "\ufeff",
                        "externalId": "e",
                        "title": "t",
                        "similarity": 0.5,
                    }
                ],
            )

    def test_build_result_requires_utc_z_timestamp(self) -> None:
        for invalid in (
            "2026-07-30T00:00:00+00:00",
            "2026-07-30T08:00:00+08:00",
            "2026-02-30T00:00:00.000Z",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                build_result("f" * 64, [], checked_at=invalid)

    def test_build_result_limits_candidate_count_and_bytes(self) -> None:
        candidate = {"source": "s", "externalId": "e", "title": "t", "similarity": 0.5}
        result = build_result(
            "1" * 64,
            [dict(candidate, externalId=str(index)) for index in range(MAX_CANDIDATES + 5)],
        )
        self.assertEqual(len(result["candidates"]), MAX_CANDIDATES)
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False).encode("utf-8")),
            MAX_RESPONSE_BYTES,
        )

    def test_v1_strips_metadata_even_when_backend_returns_it(self) -> None:
        result = build_result(
            "2" * 64,
            [
                {
                    "source": "s",
                    "externalId": "e",
                    "title": "t",
                    "similarity": 0.5,
                    "url": "https://example.invalid/x",
                    "metadata": {
                        "origin": "bzoj",
                        "contest": "noip-2010",
                    },
                }
            ],
        )
        candidate = result["candidates"][0]
        self.assertNotIn("metadata", candidate)
        self.assertEqual(
            set(candidate),
            {"source", "externalId", "title", "similarity", "url"},
        )

    def test_v2_emits_metadata_and_canonicalizes(self) -> None:
        result = build_v2_result(
            content_hash="3" * 64,
            candidates=[
                {
                    "source": "s",
                    "externalId": "e",
                    "title": "t",
                    "similarity": 0.5,
                    "metadata": {
                        "zebra": "tail",
                        "alpha": 1,
                        "beta": "中文值",
                        "flag": True,
                        "none": None,
                    },
                }
            ],
            completion={"status": "complete", "reasonCode": "complete", "retryable": False},
        )
        candidate = result["candidates"][0]
        self.assertEqual(
            candidate["metadata"],
            {
                "zebra": "tail",
                "alpha": 1,
                "beta": "中文值",
                "flag": True,
                "none": None,
            },
        )

    def test_v2_invalid_metadata_fails_closed(self) -> None:
        with self.assertRaises(ContractError):
            build_v2_result(
                "4" * 64,
                [
                    {
                        "source": "s",
                        "externalId": "e",
                        "title": "t",
                        "similarity": 0.5,
                        "metadata": {"UPPER": "rejected", "ok": "value"},
                    }
                ],
                completion={"status": "complete", "reasonCode": "complete", "retryable": False},
            )
        with self.assertRaises(ContractError):
            build_v2_result(
                "4" * 64,
                [
                    {
                        "source": "s",
                        "externalId": "e",
                        "title": "t",
                        "similarity": 0.5,
                        "metadata": {"nested": {"object": "rejected"}},
                    }
                ],
                completion={"status": "complete", "reasonCode": "complete", "retryable": False},
            )


class RankCandidateTests(unittest.TestCase):
    """查询结果只做显示下限过滤和稳定降序，不形成判定。"""

    def test_filters_below_minimum_and_sorts_descending(self) -> None:
        config = _config(minimum_similarity=0.5)
        candidates = [
            {"source": "a", "externalId": "1", "title": "high", "similarity": 0.95},
            {"source": "b", "externalId": "2", "title": "mid", "similarity": 0.6},
            {"source": "c", "externalId": "3", "title": "low", "similarity": 0.3},
        ]
        ranked = _rank_candidates(config, candidates)
        self.assertEqual(
            [candidate["similarity"] for candidate in ranked],
            [0.95, 0.6],
        )

    def test_empty_candidates_stay_empty(self) -> None:
        self.assertEqual(_rank_candidates(_config(), []), [])


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


class _FakeBackend:
    """合成后端：返回预置候选，不做任何网络调用。"""

    def __init__(self, candidates: list[dict[str, Any]] | None = None) -> None:
        self._candidates = candidates or []
        self.calls = 0

    def search(self, _query_text: str, _k: int) -> BackendSearchResult:
        self.calls += 1
        return BackendSearchResult(candidates=list(self._candidates))

    def describe_health(self) -> dict[str, Any]:
        return {
            "backend": "synthetic",
            "localProblemCount": len(self._candidates),
            "localStoreReady": True,
            "indexMetadataReady": True,
        }


class ServerTests(unittest.TestCase):
    def _service(
        self, *, candidates: list[dict[str, Any]] | None = None, **config_overrides: Any
    ) -> tuple[AnklangService, _FakeBackend]:
        config = _config(**config_overrides)
        backend = _FakeBackend(candidates)
        return AnklangService(config, backend), backend

    def test_missing_token_is_rejected(self) -> None:
        service, backend = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        status, payload = harness.request(
            "POST",
            "/api/v1/checks/similarity",
            json.dumps(_request()).encode("utf-8"),
        )
        self.assertEqual(status, 401)
        self.assertEqual(backend.calls, 0)

        wrong_headers = {
            "Authorization": "Bearer jeton-érroné",
            "Content-Type": "application/json",
        }
        wrong_status, wrong_payload = harness.request(
            "POST",
            "/api/v1/checks/similarity",
            json.dumps(_request()).encode("utf-8"),
            wrong_headers,
        )
        self.assertEqual(wrong_status, 401)
        self.assertEqual(wrong_payload, payload)
        self.assertEqual(backend.calls, 0)

    def test_unconfigured_token_allows_internal_request(self) -> None:
        service, _ = self._service(service_token=None)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        status, _ = harness.request(
            "POST",
            "/api/v1/checks/similarity",
            json.dumps(_request()).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)

    def test_similarity_flow(self) -> None:
        candidates = [
            {
                "source": "EOlymp",
                "externalId": "EOlymp/8763",
                "title": "Sum of array",
                "url": "https://eolymp.com/x",
                "similarity": 0.95,
            }
        ]
        service, backend = self._service(candidates=candidates)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request()).encode("utf-8")

        status, payload = harness.request("POST", "/api/v1/checks/similarity", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["contentHash"], "a" * 64)
        self.assertEqual(payload["apiVersion"], "1")
        self.assertEqual(payload["candidates"][0]["externalId"], "EOlymp/8763")
        self.assertEqual(backend.calls, 1)

    def test_backend_failure_returns_fixed_v1_503(self) -> None:
        class _FailingBackend:
            def __init__(self) -> None:
                self.calls = 0

            def search(self, _query_text: str, _k: int) -> BackendSearchResult:
                self.calls += 1
                raise BackendError("不应返回的内部上游信息")

            def describe_health(self) -> dict[str, Any]:
                return {"localStoreReady": False}

        config = _config()
        backend = _FailingBackend()
        service = AnklangService(config, backend)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        body = json.dumps(_request()).encode("utf-8")

        for expected_calls in (1, 2):
            status, payload = harness.request(
                "POST", "/api/v1/checks/similarity", body, headers
            )
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "CHECK_INCOMPLETE")
            self.assertNotIn("内部上游信息", json.dumps(payload, ensure_ascii=False))
            self.assertEqual(backend.calls, expected_calls)

    def test_malformed_backend_candidate_returns_safe_result(self) -> None:
        class _MalformedBackend:
            def search(self, _query_text: str, _k: int) -> BackendSearchResult:
                return BackendSearchResult(
                    candidates=[
                        {
                            "source": "example",
                            "externalId": "1",
                            "title": "不应进入降级响应的候选标题",
                            "similarity": "not-a-number",
                        }
                    ],
                )

            def describe_health(self) -> dict[str, Any]:
                return {"localStoreReady": True}

        config = _config()
        service = AnklangService(config, _MalformedBackend())
        result = service.check_similarity(parse_request(_request()))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["candidates"], [])
        self.assertNotIn("不应进入降级响应的候选标题", serialized)

    def test_health_reports_backend(self) -> None:
        service, _ = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        status, payload = harness.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "anklang")

    def test_invalid_body_is_bad_request(self) -> None:
        service, _ = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        headers = {"Authorization": "Bearer service-token-abcdef123456"}
        status, _ = harness.request("POST", "/api/v1/checks/similarity", b"not json", headers)
        self.assertEqual(status, 400)

    def test_deeply_nested_body_is_bad_request_without_leaking(self) -> None:
        service, backend = self._service()
        harness = _ServerHarness(service)
        body_text = "入站深层正文不可泄露"
        encoded_text = json.dumps(body_text, ensure_ascii=False).encode("utf-8")
        nesting_depth = 2_000
        body = (
            b'{"problem":'
            + b"[" * nesting_depth
            + b'{"basicStatement":'
            + encoded_text
            + b"}"
            + b"]" * nesting_depth
            + b"}"
        )
        self.assertLess(len(body), 4_000_000)
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                status, payload = harness.request(
                    "POST",
                    "/api/v1/checks/similarity",
                    body,
                    {
                        "Authorization": "Bearer service-token-abcdef123456",
                        "Content-Type": "application/json",
                    },
                )
        finally:
            harness.close()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(body_text, serialized)
        self.assertNotIn(body_text, stderr.getvalue())
        self.assertEqual(backend.calls, 0)

    def test_maximum_escaped_statement_is_accepted(self) -> None:
        service, _ = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        request = _request()
        request["problem"]["basicStatement"] = "\x00" * 500_000
        body = json.dumps(request).encode("utf-8")
        self.assertGreater(len(body), 2_000_000)
        self.assertLess(len(body), 4_000_000)

        status, payload = harness.request(
            "POST",
            "/api/v1/checks/similarity",
            body,
            {
                "Authorization": "Bearer service-token-abcdef123456",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["contentHash"], "a" * 64)

    def test_v2_returns_completion_and_ranked_candidates(self) -> None:
        candidates = [
            {
                "source": "EOlymp",
                "externalId": "EOlymp/1",
                "title": "Sum",
                "similarity": 0.7,
            }
        ]
        service, _ = self._service(candidates=candidates)
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        request = _request()
        request["apiVersion"] = "2"
        body = json.dumps(request).encode("utf-8")
        headers = {
            "Authorization": "Bearer service-token-abcdef123456",
            "Content-Type": "application/json",
        }
        status, payload = harness.request("POST", "/api/v2/checks/similarity", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["apiVersion"], "2")
        self.assertEqual(payload["completion"]["status"], "complete")
        self.assertEqual(
            set(payload),
            {"apiVersion", "contentHash", "checkedAt", "completion", "candidates"},
        )


if __name__ == "__main__":
    unittest.main()
