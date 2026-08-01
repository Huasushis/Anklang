"""Anklang HTTP v2 完整性与复用契约测试；全部使用合成数据。"""
from __future__ import annotations

import copy
import io
import json
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from anklang.backends import BackendError, BackendSearchResult
from anklang.backends.reverse_proxy import ReverseProxyBackend
from anklang.cache import ResultCache
from anklang.config import AppConfig
from anklang.contracts import (
    ContractError,
    build_v2_result,
    parse_request,
    validate_v2_result,
)
from anklang.llm import LlmError
from anklang.server import AnklangService, make_handler
from anklang.yuantiji import YuantijiClient


_NONCOMPLETE_REASONS = (
    "search_timeout",
    "search_rate_limited",
    "search_backend_unavailable",
    "search_backend_invalid",
    "search_partial",
    "review_unavailable",
    "service_unavailable",
    "service_invalid_response",
    "internal_error",
)


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


def _request(version: str, *, statement: str = "合成题面 alpha beta") -> dict[str, Any]:
    return {
        "apiVersion": version,
        "requestId": "44444444-4444-4444-8444-444444444444",
        "contentHash": "4" * 64,
        "problem": {
            "title": "合成测试题",
            "type": "traditional",
            "tagIds": ["synthetic"],
            "basicStatement": statement,
        },
    }


def _candidate(
    *, similarity: float = 0.95, same_problem: bool | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": "synthetic",
        "externalId": "case-1",
        "title": "合成候选",
        "similarity": similarity,
        "explanation": "合成候选说明。",
    }
    if same_problem is not None:
        result["sameProblemSuggestion"] = same_problem
    return result


class _ScriptedBackend:
    def __init__(self, *outcomes: BackendSearchResult | BaseException) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def search(self, _query_text: str, _k: int) -> BackendSearchResult:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return copy.deepcopy(outcome)

    def describe_health(self) -> dict[str, Any]:
        return {"upstreamReady": True}


class _Harness:
    def __init__(self, service: AnklangService) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.last_raw = b""

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | bytes | None = None,
        *,
        authorized: bool = True,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if isinstance(payload, dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = payload
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer service-token-abcdef123456"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            self.last_raw = raw
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
            return (
                response.status,
                decoded,
                {name.lower(): value for name, value in response.getheaders()},
            )
        finally:
            connection.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class V2ContractTests(unittest.TestCase):
    checked_at = "2026-08-01T00:00:00.000Z"
    expires_at = "2026-08-01T01:00:00.000Z"

    def _build(
        self,
        *,
        status: str = "complete",
        reason: str = "complete",
        retryable: bool = False,
        retry_after: int | None = None,
        candidates: list[dict[str, Any]] | None = None,
        block: bool = False,
        reuse: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        completion: dict[str, Any] = {
            "status": status,
            "reasonCode": reason,
            "retryable": retryable,
        }
        if retry_after is not None:
            completion["retryAfterSeconds"] = retry_after
        return build_v2_result(
            content_hash="a" * 64,
            candidates=[] if candidates is None else candidates,
            block_submission=block,
            message="合成结果说明。",
            completion=completion,
            reuse=(
                {"policy": "allowed", "expiresAt": self.expires_at}
                if reuse is None and status == "complete"
                else (reuse or {"policy": "no-store"})
            ),
            checked_at=self.checked_at,
        )

    def test_complete_branch_has_exact_shape(self) -> None:
        result = self._build(candidates=[_candidate()])
        self.assertEqual(
            set(result),
            {
                "apiVersion",
                "contentHash",
                "checkedAt",
                "completion",
                "candidates",
                "recommendation",
                "reuse",
            },
        )
        self.assertEqual(
            result["completion"],
            {"status": "complete", "reasonCode": "complete", "retryable": False},
        )
        self.assertEqual(
            result["reuse"],
            {"policy": "allowed", "expiresAt": self.expires_at},
        )
        self.assertEqual(validate_v2_result(result), result)

    def test_noncomplete_reason_and_status_matrix(self) -> None:
        for status in ("partial", "unavailable"):
            for reason in _NONCOMPLETE_REASONS:
                with self.subTest(status=status, reason=reason):
                    result = self._build(
                        status=status,
                        reason=reason,
                        retryable=True,
                        retry_after=17,
                    )
                    self.assertEqual(result["completion"]["status"], status)
                    self.assertEqual(result["completion"]["reasonCode"], reason)
                    self.assertEqual(result["completion"]["retryAfterSeconds"], 17)
                    self.assertEqual(result["reuse"], {"policy": "no-store"})

    def test_cross_branch_constraints_reject_unsafe_results(self) -> None:
        invalid_builds = (
            lambda: self._build(
                status="unavailable",
                reason="service_unavailable",
                candidates=[_candidate()],
            ),
            lambda: self._build(
                status="unavailable",
                reason="service_unavailable",
                block=True,
            ),
            lambda: self._build(
                status="partial",
                reason="search_partial",
                candidates=[_candidate()],
                block=True,
            ),
            lambda: self._build(
                status="partial",
                reason="search_partial",
                reuse={"policy": "allowed", "expiresAt": self.expires_at},
            ),
            lambda: self._build(status="complete", reason="search_partial"),
            lambda: self._build(
                status="partial",
                reason="search_partial",
                retryable=False,
                retry_after=5,
            ),
        )
        for index, build in enumerate(invalid_builds):
            with self.subTest(case=index), self.assertRaises(ContractError):
                build()

        trusted_partial = self._build(
            status="partial",
            reason="search_partial",
            candidates=[_candidate(same_problem=True)],
            block=True,
        )
        self.assertTrue(trusted_partial["recommendation"]["blockSubmission"])

    def test_reuse_expiry_is_strictly_bounded(self) -> None:
        exactly_seven_days = "2026-08-08T00:00:00.000Z"
        result = self._build(
            reuse={"policy": "allowed", "expiresAt": exactly_seven_days}
        )
        self.assertEqual(result["reuse"]["expiresAt"], exactly_seven_days)
        for invalid in (
            self.checked_at,
            "2026-08-08T00:00:00.001Z",
            "2026-08-01T01:00:00+00:00",
        ):
            with self.subTest(expires_at=invalid), self.assertRaises(ContractError):
                self._build(reuse={"policy": "allowed", "expiresAt": invalid})

    def test_validator_rejects_extra_fields_at_every_v2_object_level(self) -> None:
        for mutate in (
            lambda value: value.update({"extra": True}),
            lambda value: value["completion"].update({"extra": True}),
            lambda value: value["recommendation"].update({"extra": True}),
            lambda value: value["reuse"].update({"extra": True}),
            lambda value: value["candidates"][0].update({"extra": True}),
        ):
            value = self._build(candidates=[_candidate()])
            mutate(value)
            with self.assertRaises(ContractError):
                validate_v2_result(value)

    def test_v2_request_version_and_keys_are_strict(self) -> None:
        parsed = parse_request(_request("2"), expected_api_version="2")
        self.assertEqual(parsed["content_hash"], "4" * 64)
        for invalid in (_request("1"), {**_request("2"), "extra": True}):
            with self.assertRaises(ContractError):
                parse_request(invalid, expected_api_version="2")


class CacheContractTests(unittest.TestCase):
    def test_cache_uses_absolute_expiry_without_refresh_and_returns_copies(self) -> None:
        monotonic = {"now": 10.0}
        wall = {"now": datetime(2026, 8, 1, tzinfo=timezone.utc)}
        cache = ResultCache(
            ttl_seconds=10,
            max_entries=2,
            clock=lambda: monotonic["now"],
            wall_clock=lambda: wall["now"],
        )
        checked_at = "2026-08-01T00:00:00.000Z"
        value = {
            "checkedAt": checked_at,
            "completion": {"status": "complete"},
            "reuse": {"policy": "allowed"},
            "nested": {"value": 1},
        }
        self.assertEqual(
            cache.expires_at_for(checked_at),
            "2026-08-01T00:00:10.000Z",
        )
        cache.set("key", value, checked_at=checked_at)

        first = cache.get("key")
        assert first is not None
        first["nested"]["value"] = 99
        self.assertEqual(cache.get("key")["nested"]["value"], 1)  # type: ignore[index]

        monotonic["now"] = 15.0
        wall["now"] += timedelta(seconds=5)
        self.assertIsNotNone(cache.get("key"))
        # 命中没有把绝对到期时间向后推；墙钟到原 expiresAt 即失效。
        monotonic["now"] = 19.0
        wall["now"] += timedelta(seconds=5)
        self.assertIsNone(cache.get("key"))

    def test_cache_refuses_ttl_over_seven_days(self) -> None:
        with self.assertRaises(ValueError):
            ResultCache(ttl_seconds=604_801, max_entries=1)

    def test_backward_wall_clock_cannot_extend_monotonic_ttl(self) -> None:
        monotonic = {"now": 0.0}
        wall = {"now": datetime(2026, 7, 31, tzinfo=timezone.utc)}
        cache = ResultCache(
            ttl_seconds=10,
            max_entries=1,
            clock=lambda: monotonic["now"],
            wall_clock=lambda: wall["now"],
        )
        cache.set(
            "key",
            {
                "checkedAt": "2026-08-01T00:00:00.000Z",
                "completion": {"status": "complete"},
                "reuse": {"policy": "allowed"},
            },
            checked_at="2026-08-01T00:00:00.000Z",
        )
        monotonic["now"] = 10.0
        self.assertIsNone(cache.get("key"))

    def test_cache_rejects_nonreusable_values(self) -> None:
        cache = ResultCache(ttl_seconds=10, max_entries=1)
        for status, policy in (
            ("partial", "no-store"),
            ("unavailable", "no-store"),
            ("complete", "no-store"),
        ):
            with self.subTest(status=status, policy=policy), self.assertRaises(ValueError):
                cache.set(
                    "key",
                    {
                        "completion": {"status": status},
                        "reuse": {"policy": policy},
                    },
                )
        self.assertIsNone(cache.get("key"))


class HttpV2Tests(unittest.TestCase):
    def _harness(
        self,
        backend: _ScriptedBackend,
        *,
        llm_client: Any = None,
        **config_overrides: Any,
    ) -> tuple[_Harness, AnklangService]:
        config = _config(**config_overrides)
        service = AnklangService(
            config,
            backend,
            ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
            llm_client,
        )
        harness = _Harness(service)
        self.addCleanup(harness.close)
        return harness, service

    def test_complete_v2_is_cached_with_original_checked_at_and_expiry(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([_candidate()]))
        harness, _ = self._harness(backend)

        first_status, first, first_headers = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        second_status, second, second_headers = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )

        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(backend.calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["completion"], {
            "status": "complete",
            "reasonCode": "complete",
            "retryable": False,
        })
        self.assertEqual(first["reuse"]["policy"], "allowed")
        checked = datetime.fromisoformat(first["checkedAt"][:-1] + "+00:00")
        expires = datetime.fromisoformat(first["reuse"]["expiresAt"][:-1] + "+00:00")
        self.assertGreater(expires, checked)
        self.assertLessEqual(expires - checked, timedelta(days=7))
        self.assertEqual(first_headers.get("cache-control"), "no-store")
        self.assertEqual(second_headers.get("cache-control"), "no-store")

    def test_v1_keeps_old_success_shape_and_has_separate_cache_namespace(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([_candidate()]))
        harness, _ = self._harness(backend)

        v2_status, _, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        v1_status, v1, headers = harness.request(
            "POST", "/api/v1/checks/similarity", _request("1")
        )
        repeat_status, repeat, _ = harness.request(
            "POST", "/api/v1/checks/similarity", _request("1")
        )

        self.assertEqual((v2_status, v1_status, repeat_status), (200, 200, 200))
        self.assertEqual(backend.calls, 2)
        self.assertEqual(v1, repeat)
        self.assertEqual(
            set(v1),
            {"apiVersion", "contentHash", "checkedAt", "candidates", "recommendation"},
        )
        self.assertEqual(v1["apiVersion"], "1")
        self.assertNotIn("completion", v1)
        self.assertNotIn("reuse", v1)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_same_claimed_hash_with_different_statement_does_not_share_cache(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([_candidate()]))
        harness, _ = self._harness(backend)
        for statement in ("合成题面 one", "合成题面 two"):
            status, _, _ = harness.request(
                "POST",
                "/api/v2/checks/similarity",
                _request("2", statement=statement),
            )
            self.assertEqual(status, 200)
        self.assertEqual(backend.calls, 2)

    def test_partial_does_not_threshold_block_or_enter_cache(self) -> None:
        backend = _ScriptedBackend(
            BackendSearchResult.partial([_candidate(similarity=0.99)])
        )
        harness, _ = self._harness(
            backend,
            similarity_block_enabled=True,
            block_threshold=0.9,
        )
        for _ in range(2):
            status, payload, headers = harness.request(
                "POST", "/api/v2/checks/similarity", _request("2")
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["completion"]["status"], "partial")
            self.assertFalse(payload["recommendation"]["blockSubmission"])
            self.assertEqual(payload["reuse"], {"policy": "no-store"})
            self.assertTrue(payload["candidates"])
            self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(backend.calls, 2)

    def test_backend_cannot_inject_trusted_same_problem_suggestion(self) -> None:
        backend = _ScriptedBackend(
            BackendSearchResult.partial([_candidate(same_problem=True)])
        )
        harness, _ = self._harness(backend, similarity_block_enabled=True)
        status, payload, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["recommendation"]["blockSubmission"])
        self.assertNotIn("sameProblemSuggestion", payload["candidates"][0])

    def test_malformed_partial_decision_cannot_claim_trusted_block(self) -> None:
        backend = _ScriptedBackend(
            BackendSearchResult.partial([_candidate(same_problem=True)])
        )
        harness, _ = self._harness(backend)
        with patch(
            "anklang.server.evaluate",
            return_value={
                "candidates": [_candidate(same_problem=True)],
                "block_submission": True,
                "message": "合成内部结果。",
                "review_failed": False,
                "trusted_same_problem": False,
            },
        ):
            status, payload, _ = harness.request(
                "POST", "/api/v2/checks/similarity", _request("2")
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["completion"]["status"], "unavailable")
        self.assertEqual(
            payload["completion"]["reasonCode"], "service_invalid_response"
        )
        self.assertFalse(payload["recommendation"]["blockSubmission"])

    def test_partial_can_block_only_with_trusted_positive_review(self) -> None:
        class _PositiveReview:
            def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
                return {"sameProblem": True}

        backend = _ScriptedBackend(
            BackendSearchResult.partial([_candidate(similarity=0.99)])
        )
        harness, _ = self._harness(
            backend,
            llm_client=_PositiveReview(),
            llm_review_enabled=True,
            llm_base_url="https://review.test",
            llm_api_key="synthetic-key",
            similarity_block_enabled=True,
        )
        status, payload, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["completion"]["status"], "partial")
        self.assertTrue(payload["recommendation"]["blockSubmission"])
        self.assertTrue(payload["candidates"][0]["sameProblemSuggestion"])

    def test_configured_review_partial_failure_marks_result_partial(self) -> None:
        class _PartlyFailingReview:
            def __init__(self) -> None:
                self.calls = 0

            def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 1:
                    return {"sameProblem": False}
                raise LlmError("synthetic failure")

        candidates = [
            _candidate(similarity=0.99),
            {
                **_candidate(similarity=0.98),
                "externalId": "case-2",
                "title": "合成候选二",
            },
        ]
        backend = _ScriptedBackend(BackendSearchResult(candidates))
        harness, _ = self._harness(
            backend,
            llm_client=_PartlyFailingReview(),
            llm_review_enabled=True,
            llm_base_url="https://review.test",
            llm_api_key="synthetic-key",
            similarity_block_enabled=True,
        )
        status, payload, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["completion"]["status"], "partial")
        self.assertEqual(payload["completion"]["reasonCode"], "review_unavailable")
        self.assertFalse(payload["recommendation"]["blockSubmission"])
        self.assertEqual(payload["reuse"], {"policy": "no-store"})

    def test_unavailable_v2_is_200_empty_and_never_cached(self) -> None:
        backend = _ScriptedBackend(
            BackendSearchResult.unavailable(
                reason_code="search_rate_limited",
                retryable=True,
                retry_after_seconds=23,
            )
        )
        harness, _ = self._harness(backend)
        for _ in range(2):
            status, payload, headers = harness.request(
                "POST", "/api/v2/checks/similarity", _request("2")
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["candidates"], [])
            self.assertFalse(payload["recommendation"]["blockSubmission"])
            self.assertEqual(payload["completion"]["reasonCode"], "search_rate_limited")
            self.assertEqual(payload["completion"]["retryAfterSeconds"], 23)
            self.assertEqual(payload["reuse"], {"policy": "no-store"})
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("retry-after"), "23")
        self.assertEqual(backend.calls, 2)

    def test_v1_noncomplete_is_fixed_503_without_candidate_leak(self) -> None:
        backend = _ScriptedBackend(
            BackendSearchResult.partial(
                [{**_candidate(), "title": "不应出现在 v1 错误中的合成标记"}]
            )
        )
        harness, _ = self._harness(backend)
        status, payload, headers = harness.request(
            "POST", "/api/v1/checks/similarity", _request("1")
        )
        self.assertEqual(status, 503)
        self.assertEqual(
            payload,
            {
                "error": {
                    "code": "CHECK_INCOMPLETE",
                    "message": "本次未能完成原题检索，请稍后重试并人工核对。",
                }
            },
        )
        self.assertNotIn("合成标记", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_auth_body_and_version_errors_are_fixed_and_no_store(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([]))
        harness, _ = self._harness(backend)
        cases = (
            ("/api/v2/checks/similarity", _request("2"), False, 401),
            ("/api/v2/checks/similarity", b"not-json", True, 400),
            ("/api/v2/checks/similarity", _request("1"), True, 400),
            ("/api/v1/checks/similarity", _request("2"), True, 400),
        )
        for path, body, authorized, expected in cases:
            with self.subTest(path=path, expected=expected):
                status, payload, headers = harness.request(
                    "POST", path, body, authorized=authorized
                )
                self.assertEqual(status, expected)
                self.assertIn("error", payload)
                self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(backend.calls, 0)

        for path in (
            "/api/v1/checks/similarity",
            "/api/v2/checks/similarity",
        ):
            for method in (
                "GET",
                "HEAD",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
                "TRACE",
                "CONNECT",
                "SYNTHETIC",
            ):
                with self.subTest(path=path, method=method):
                    status, payload, headers = harness.request(method, path)
                    self.assertEqual(status, 405)
                    if method == "HEAD":
                        self.assertEqual(harness.last_raw, b"")
                        self.assertGreater(int(headers["content-length"]), 0)
                    else:
                        self.assertEqual(
                            payload["error"]["code"], "METHOD_NOT_ALLOWED"
                        )
                    self.assertEqual(headers.get("cache-control"), "no-store")

    def test_all_health_not_found_and_path_variant_responses_are_no_store(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([]))
        harness, _ = self._harness(backend)
        cases = (
            ("GET", "/api/v1/health", 200),
            ("GET", "/unrelated", 404),
            ("SYNTHETIC", "/unrelated", 405),
            ("POST", "/api/v2/checks/similarity?query=1", 404),
            ("POST", "/api/v2/checks/similarity/", 404),
        )
        for method, path, expected in cases:
            with self.subTest(method=method, path=path):
                status, _, headers = harness.request(method, path)
                self.assertEqual(status, expected)
                self.assertEqual(headers.get("cache-control"), "no-store")
                self.assertNotIn("Python", headers.get("server", ""))

    def test_untrusted_internal_result_becomes_fixed_500_no_store(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([]))
        harness, service = self._harness(backend)

        def _bad_result(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"private": "synthetic-internal-marker"}

        service.check_similarity = _bad_result  # type: ignore[method-assign]
        status, payload, headers = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "INVALID_RESULT")
        self.assertNotIn("synthetic-internal-marker", json.dumps(payload))
        self.assertEqual(headers.get("cache-control"), "no-store")


class UpstreamClassificationTests(unittest.TestCase):
    class _Opener:
        def __init__(self, outcome: Any) -> None:
            self.outcome = outcome

        def __call__(self, _request: Any, timeout: float) -> Any:  # noqa: ARG002
            outcome = self.outcome
            if isinstance(outcome, BaseException):
                raise outcome
            raw = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode()

            class _Response:
                def __enter__(self_inner) -> "UpstreamClassificationTests._Opener._Response":
                    return self_inner  # type: ignore[return-value]

                def __exit__(self_inner, *_args: Any) -> None:
                    return None

                def read(self_inner, _limit: int) -> bytes:
                    return raw

            return _Response()

    def _search(self, outcome: Any) -> BackendSearchResult:
        client = YuantijiClient(
            "https://yuantiji.test",
            timeout_seconds=1.0,
            minimum_interval_seconds=0.0,
            opener=self._Opener(outcome),
            max_retries=0,
            request_queue_seconds=0.1,
        )
        return ReverseProxyBackend(client, use_rerank=False).search("synthetic", 8)

    def test_timeout_rate_limit_unavailable_and_invalid_have_fixed_reasons(self) -> None:
        headers = Message()
        headers["Retry-After"] = "17"
        scenarios = (
            (TimeoutError(), "search_timeout", True, None),
            (
                urllib.error.HTTPError(
                    "https://yuantiji.test/api/search",
                    429,
                    "rate limited",
                    headers,
                    io.BytesIO(b""),
                ),
                "search_rate_limited",
                True,
                17,
            ),
            (
                urllib.error.HTTPError(
                    "https://yuantiji.test/api/search",
                    503,
                    "unavailable",
                    Message(),
                    io.BytesIO(b""),
                ),
                "search_backend_unavailable",
                True,
                None,
            ),
            (b"not-json", "search_backend_invalid", False, None),
            ({"unexpected": []}, "search_backend_invalid", False, None),
        )
        for outcome, reason, retryable, retry_after in scenarios:
            with self.subTest(reason=reason, outcome=type(outcome).__name__):
                result = self._search(outcome)
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.reason_code, reason)
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(result.retry_after_seconds, retry_after)

    def test_mixed_malformed_candidates_are_partial_not_complete(self) -> None:
        result = self._search(
            {
                "results": [
                    {
                        "uid": "synthetic/1",
                        "title": "合成候选",
                        "src": "synthetic",
                        "cos": 0.8,
                    },
                    {"uid": "missing-required-fields"},
                ]
            }
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.reason_code, "search_backend_invalid")
        self.assertEqual(len(result.candidates), 1)


class BackendStateInvariantTests(unittest.TestCase):
    def test_backend_state_rejects_ambiguous_combinations(self) -> None:
        with self.assertRaises(ValueError):
            BackendSearchResult([], status="complete", reason_code="search_partial")
        with self.assertRaises(ValueError):
            BackendSearchResult(
                [_candidate()],
                status="unavailable",
                reason_code="service_unavailable",
                retryable=True,
            )
        with self.assertRaises(ValueError):
            BackendSearchResult.partial([], retryable=False, retry_after_seconds=2)

    def test_backend_exception_metadata_never_requires_exposing_message(self) -> None:
        error = BackendError(
            "synthetic-private-detail",
            reason_code="service_unavailable",
            retryable=True,
        )
        self.assertEqual(error.reason_code, "service_unavailable")
        self.assertTrue(error.retryable)


if __name__ == "__main__":
    unittest.main()
