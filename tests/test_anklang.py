"""Anklang 阶段 1 测试。仅用标准库 unittest，不做真实网络调用。

运行：python -m unittest discover -s tests
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import unittest
from typing import Any

from anklang.backends import BackendError, BackendSearchResult
from anklang.backends.reverse_proxy import ReverseProxyBackend
from anklang.cache import ResultCache
from anklang.config import AppConfig
from anklang.contracts import (
    MAX_CANDIDATES,
    MAX_RESPONSE_BYTES,
    ContractError,
    build_result,
    parse_request,
)
from anklang.llm import LlmClient
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
        "requestId": "11111111-1111-4111-8111-111111111111",
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
            block_submission=True,
            message="发现相似题",
        )
        self.assertEqual(result["apiVersion"], "1")
        self.assertEqual(result["contentHash"], "b" * 64)
        self.assertRegex(
            result["checkedAt"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        )
        self.assertTrue(result["recommendation"]["blockSubmission"])
        self.assertEqual(result["candidates"][0]["similarity"], 0.9)
        self.assertEqual(
            set(result),
            {"apiVersion", "contentHash", "checkedAt", "candidates", "recommendation"},
        )

    def test_build_result_rejects_out_of_range_similarity(self) -> None:
        for value in (1.5, -0.1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ContractError):
                build_result(
                    "c" * 64,
                    [{"source": "s", "externalId": "e", "title": "t", "similarity": value}],
                    False,
                    "x",
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
                build_result("d" * 64, [candidate], False, "x")

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
                    False,
                    "已完成",
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
                False,
                "已完成",
            )

    def test_build_result_requires_utc_z_timestamp(self) -> None:
        for invalid in (
            "2026-07-30T00:00:00+00:00",
            "2026-07-30T08:00:00+08:00",
            "2026-02-30T00:00:00.000Z",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                build_result("f" * 64, [], False, "已完成", checked_at=invalid)

    def test_build_result_limits_candidate_count_and_bytes(self) -> None:
        candidate = {"source": "s", "externalId": "e", "title": "t", "similarity": 0.5}
        result = build_result(
            "1" * 64,
            [dict(candidate, externalId=str(index)) for index in range(MAX_CANDIDATES + 5)],
            False,
            "已完成",
        )
        self.assertEqual(len(result["candidates"]), MAX_CANDIDATES)
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False).encode("utf-8")),
            MAX_RESPONSE_BYTES,
        )


class MappingTests(unittest.TestCase):
    def test_prefers_rerank_over_cosine_and_clamps(self) -> None:
        mapped = YuantijiClient._map_candidate(
            {"uid": "X/1", "title": "T", "src": "X", "url": "https://x", "cos": 0.7, "rr": 1.3, "original": "abc"}
        )
        self.assertIsNotNone(mapped)
        assert mapped is not None
        self.assertEqual(mapped["similarity"], 1.0)
        self.assertEqual(mapped["externalId"], "X/1")
        self.assertNotIn("abc", mapped["explanation"])

    def test_falls_back_to_cosine_when_no_rerank(self) -> None:
        mapped = YuantijiClient._map_candidate(
            {"uid": "Y/2", "title": "T", "src": "Y", "cos": 0.42, "rr": None}
        )
        self.assertIsNotNone(mapped)
        assert mapped is not None
        self.assertAlmostEqual(mapped["similarity"], 0.42)

    def test_skips_candidate_without_external_id(self) -> None:
        self.assertIsNone(YuantijiClient._map_candidate({"title": "T", "cos": 0.8}))

    def test_skips_candidate_without_required_text_or_score(self) -> None:
        for candidate in (
            {"uid": "X/1", "title": "T", "src": "", "cos": 0.8},
            {"uid": "X/1", "title": "", "src": "X", "cos": 0.8},
            {"uid": "X/1", "title": "T", "src": "X"},
            {"uid": "X/1", "title": "T", "src": "X", "cos": True},
        ):
            with self.subTest(candidate=candidate):
                self.assertIsNone(YuantijiClient._map_candidate(candidate))


class ReviewTests(unittest.TestCase):
    def test_filters_below_minimum_and_blocks_above_threshold(self) -> None:
        config = _config(
            minimum_similarity=0.5,
            block_threshold=0.9,
            similarity_block_enabled=True,
        )
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
        config = _config(
            minimum_similarity=0.5,
            block_threshold=0.9,
            similarity_block_enabled=True,
        )
        candidates = [{"source": "a", "externalId": "1", "title": "mid", "similarity": 0.6}]
        decision = evaluate(config, {"title": "t", "basic_statement": "s"}, candidates, None)
        self.assertFalse(decision["block_submission"])

    def test_default_does_not_block_on_uncalibrated_similarity(self) -> None:
        config = _config(minimum_similarity=0.5, block_threshold=0.9)
        candidates = [{"source": "a", "externalId": "1", "title": "high", "similarity": 0.99}]
        decision = evaluate(config, {"title": "t", "basic_statement": "s"}, candidates, None)
        self.assertFalse(decision["block_submission"])
        self.assertIn("未启用", decision["message"])

    def test_llm_explanation_cannot_copy_candidate_statement_to_result(self) -> None:
        class _ReviewClient:
            def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
                return {
                    "sameProblem": True,
                    "explanation": "不应采纳的候选题面原句",
                }

        candidate = {
            "source": "example",
            "externalId": "1",
            "title": "candidate",
            "similarity": 0.99,
            "explanation": "初始固定说明",
            "_reviewExcerpt": "只供本次模型复核的候选正文",
        }
        decision = evaluate(
            _config(llm_review_enabled=True),
            {"title": "submitted", "basic_statement": "synthetic statement"},
            [candidate],
            _ReviewClient(),  # type: ignore[arg-type]
        )
        result = build_result(
            "2" * 64,
            decision["candidates"],
            decision["block_submission"],
            decision["message"],
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertTrue(result["recommendation"]["blockSubmission"])
        self.assertIn("模型复核认为", result["candidates"][0]["explanation"])
        self.assertNotIn("不应采纳的候选题面原句", serialized)
        self.assertNotIn("只供本次模型复核的候选正文", serialized)


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
                        {
                            "uid": "EOlymp/8763",
                            "title": "Sum of array",
                            "src": "EOlymp",
                            "url": "https://x",
                            "cos": 0.95,
                            "rr": None,
                            "original": "不应返回到主系统的候选正文样例",
                        },
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
        service, opener = self._service()
        harness = _ServerHarness(service)
        self.addCleanup(harness.close)
        status, payload = harness.request(
            "POST",
            "/api/v1/checks/similarity",
            json.dumps(_request()).encode("utf-8"),
        )
        self.assertEqual(status, 401)
        self.assertEqual(opener.calls, [])

        # HTTP/1.1 请求头只能直接承载 Latin-1 字符；带重音符号的错误令牌仍能
        # 覆盖旧版 compare_digest 遇到非 ASCII 文本会抛异常的问题。
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
        self.assertEqual(opener.calls, [])

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
        self.assertFalse(payload["recommendation"]["blockSubmission"])
        self.assertEqual(payload["candidates"][0]["externalId"], "EOlymp/8763")
        self.assertNotIn(
            "不应返回到主系统的候选正文样例",
            json.dumps(payload, ensure_ascii=False),
        )
        search_calls = [call for call in opener.calls if call.endswith("/api/search")]
        self.assertEqual(len(search_calls), 1)

        # 第二次相同摘要走缓存，不再打上游。
        status2, payload2 = harness.request("POST", "/api/v1/checks/similarity", body, headers)
        self.assertEqual(status2, 200)
        self.assertEqual(payload2["contentHash"], "a" * 64)
        search_calls_after = [call for call in opener.calls if call.endswith("/api/search")]
        self.assertEqual(len(search_calls_after), 1)

    def test_backend_failure_returns_safe_200_without_caching(self) -> None:
        class _FailingBackend:
            calls = 0

            def search(self, _query_text: str, _k: int) -> BackendSearchResult:
                self.calls += 1
                raise BackendError("不应返回的内部上游信息")

            def describe_health(self) -> dict[str, Any]:
                return {"upstreamReady": False}

        config = _config()
        backend = _FailingBackend()
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
        body = json.dumps(_request()).encode("utf-8")

        for expected_calls in (1, 2):
            status, payload = harness.request(
                "POST", "/api/v1/checks/similarity", body, headers
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["contentHash"], "a" * 64)
            self.assertEqual(payload["candidates"], [])
            self.assertFalse(payload["recommendation"]["blockSubmission"])
            self.assertNotIn("内部上游信息", json.dumps(payload, ensure_ascii=False))
            self.assertEqual(backend.calls, expected_calls)

    def test_llm_read_failure_returns_safe_200_without_caching(self) -> None:
        submitted_statement = "投题题面不可泄露标记"
        candidate_excerpt = "候选正文不可泄露标记"
        external_error = "模型服务错误不可泄露标记"

        class _CandidateBackend:
            def __init__(self) -> None:
                self.calls = 0

            def search(self, _query_text: str, _k: int) -> BackendSearchResult:
                self.calls += 1
                return BackendSearchResult(
                    candidates=[
                        {
                            "source": "example",
                            "externalId": "example/1",
                            "title": "公开候选标题",
                            "similarity": 0.99,
                            "_reviewExcerpt": candidate_excerpt,
                        }
                    ],
                    degraded=False,
                )

            def describe_health(self) -> dict[str, Any]:
                return {"upstreamReady": True}

        class _InterruptedOpener:
            def __init__(self, error_factory: Any) -> None:
                self.error_factory = error_factory
                self.calls = 0

            def __call__(self, _request: Any, timeout: float) -> Any:  # noqa: ARG002
                self.calls += 1
                error_factory = self.error_factory

                class _Response:
                    def __enter__(self_inner) -> "_Response":
                        return self_inner

                    def __exit__(self_inner, *_args: Any) -> None:
                        return None

                    def read(self_inner, _limit: int) -> bytes:
                        raise error_factory()

                return _Response()

        failures = (
            ("os-error", lambda: OSError(external_error)),
            (
                "incomplete-read",
                lambda: http.client.IncompleteRead(external_error.encode("utf-8"), 100),
            ),
        )
        for label, error_factory in failures:
            with self.subTest(failure=label):
                config = _config(llm_review_enabled=True)
                backend = _CandidateBackend()
                opener = _InterruptedOpener(error_factory)
                service = AnklangService(
                    config,
                    backend,
                    ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
                    LlmClient("https://llm.test", "synthetic-key", opener=opener),
                )
                harness = _ServerHarness(service)
                request = _request()
                request["problem"]["basicStatement"] = submitted_statement
                body = json.dumps(request).encode("utf-8")
                headers = {
                    "Authorization": "Bearer service-token-abcdef123456",
                    "Content-Type": "application/json",
                }
                responses: list[dict[str, Any]] = []
                stderr = io.StringIO()
                try:
                    with contextlib.redirect_stderr(stderr):
                        for _ in range(2):
                            status, payload = harness.request(
                                "POST",
                                "/api/v1/checks/similarity",
                                body,
                                headers,
                            )
                            self.assertEqual(status, 200)
                            self.assertEqual(payload["contentHash"], "a" * 64)
                            self.assertFalse(
                                payload["recommendation"]["blockSubmission"]
                            )
                            self.assertEqual(
                                payload["candidates"][0]["title"],
                                "公开候选标题",
                            )
                            responses.append(payload)
                finally:
                    harness.close()

                self.assertEqual(backend.calls, 2)
                self.assertEqual(opener.calls, 2)
                serialized = json.dumps(responses, ensure_ascii=False)
                captured_stderr = stderr.getvalue()
                for secret in (
                    submitted_statement,
                    candidate_excerpt,
                    external_error,
                ):
                    self.assertNotIn(secret, serialized)
                    self.assertNotIn(secret, captured_stderr)
                self.assertNotIn("review_failed", serialized)
                self.assertNotIn("review_failed", captured_stderr)

    def test_llm_json_limits_return_safe_200_without_caching(self) -> None:
        submitted_statement = "深层测试投题题面不可泄露"
        candidate_excerpt = "深层测试候选正文不可泄露"
        deep_payload_text = "深层模型响应原文不可泄露"
        nesting_depth = 2_000

        class _CandidateBackend:
            def __init__(self) -> None:
                self.calls = 0

            def search(self, _query_text: str, _k: int) -> BackendSearchResult:
                self.calls += 1
                return BackendSearchResult(
                    candidates=[
                        {
                            "source": "example",
                            "externalId": "example/deep",
                            "title": "公开候选标题",
                            "similarity": 0.99,
                            "_reviewExcerpt": candidate_excerpt,
                        }
                    ],
                    degraded=False,
                )

            def describe_health(self) -> dict[str, Any]:
                return {"upstreamReady": True}

        class _BodyOpener:
            def __init__(self, body: bytes) -> None:
                self.body = body
                self.calls = 0

            def __call__(self, _request: Any, timeout: float) -> Any:  # noqa: ARG002
                self.calls += 1
                body = self.body

                class _Response:
                    def __enter__(self_inner) -> "_Response":
                        return self_inner

                    def __exit__(self_inner, *_args: Any) -> None:
                        return None

                    def read(self_inner, _limit: int) -> bytes:
                        return body

                return _Response()

        encoded_marker = json.dumps(
            deep_payload_text,
            ensure_ascii=False,
        ).encode("utf-8")
        deep_outer_response = (
            b"[" * nesting_depth
            + encoded_marker
            + b"]" * nesting_depth
        )
        deep_content = (
            '{"sameProblem":'
            + "[" * nesting_depth
            + json.dumps(deep_payload_text, ensure_ascii=False)
            + "]" * nesting_depth
            + "}"
        )
        deep_content_response = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": deep_content,
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        long_integer = "9" * 5_000
        long_integer_outer_response = (
            b'{"marker":'
            + encoded_marker
            + b',"value":'
            + long_integer.encode("ascii")
            + b"}"
        )
        long_integer_content_response = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"sameProblem":'
                                + long_integer
                                + ',"marker":'
                                + json.dumps(deep_payload_text, ensure_ascii=False)
                                + "}"
                            ),
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")

        response_cases = (
            ("outer-response-depth", deep_outer_response),
            ("message-content-depth", deep_content_response),
            ("outer-response-long-integer", long_integer_outer_response),
            ("message-content-long-integer", long_integer_content_response),
        )
        for _, response_body in response_cases:
            self.assertLess(len(response_body), 2_000_000)

        for label, response_body in response_cases:
            with self.subTest(failure=label):
                config = _config(llm_review_enabled=True)
                backend = _CandidateBackend()
                opener = _BodyOpener(response_body)
                service = AnklangService(
                    config,
                    backend,
                    ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
                    LlmClient("https://llm.test", "synthetic-key", opener=opener),
                )
                harness = _ServerHarness(service)
                request = _request()
                request["problem"]["basicStatement"] = submitted_statement
                body = json.dumps(request).encode("utf-8")
                headers = {
                    "Authorization": "Bearer service-token-abcdef123456",
                    "Content-Type": "application/json",
                }
                responses: list[dict[str, Any]] = []
                stderr = io.StringIO()
                try:
                    with contextlib.redirect_stderr(stderr):
                        for _ in range(2):
                            status, payload = harness.request(
                                "POST",
                                "/api/v1/checks/similarity",
                                body,
                                headers,
                            )
                            self.assertEqual(status, 200)
                            self.assertEqual(payload["contentHash"], "a" * 64)
                            self.assertFalse(
                                payload["recommendation"]["blockSubmission"]
                            )
                            self.assertEqual(
                                payload["candidates"][0]["title"],
                                "公开候选标题",
                            )
                            responses.append(payload)
                finally:
                    harness.close()

                self.assertEqual(backend.calls, 2)
                self.assertEqual(opener.calls, 2)
                serialized = json.dumps(responses, ensure_ascii=False)
                captured_stderr = stderr.getvalue()
                for secret in (
                    submitted_statement,
                    candidate_excerpt,
                    deep_payload_text,
                ):
                    self.assertNotIn(secret, serialized)
                    self.assertNotIn(secret, captured_stderr)
                self.assertNotIn("review_failed", serialized)
                self.assertNotIn("review_failed", captured_stderr)

    def test_malformed_backend_candidate_returns_safe_200(self) -> None:
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
                    degraded=False,
                )

            def describe_health(self) -> dict[str, Any]:
                return {"upstreamReady": True}

        config = _config()
        service = AnklangService(
            config,
            _MalformedBackend(),
            ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
            None,
        )
        result = service.check_similarity(parse_request(_request()))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["recommendation"]["blockSubmission"])
        self.assertNotIn("不应进入降级响应的候选标题", serialized)

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

    def test_deeply_nested_body_is_bad_request_without_leaking(self) -> None:
        service, opener = self._service()
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
        self.assertEqual(opener.calls, [])

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


if __name__ == "__main__":
    unittest.main()
