"""Anklang HTTP v2 完整性与复用契约测试；全部使用合成数据。"""
from __future__ import annotations

import copy
import io
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from anklang.backends import BackendError, BackendSearchResult
from anklang.config import AppConfig
from anklang.contracts import (
    ContractError,
    build_v2_result,
    parse_request,
    validate_v2_result,
)
from anklang.http_api import AnklangService, make_handler


_NONCOMPLETE_REASONS = (
    "search_timeout",
    "search_rate_limited",
    "search_backend_unavailable",
    "search_backend_invalid",
    "search_partial",
    "service_unavailable",
    "service_invalid_response",
    "internal_error",
)


def _config(**overrides: Any) -> AppConfig:
    values = dict(
        port=8730,
        service_token="service-token-abcdef123456",
        search_k=8,
        minimum_similarity=0.5,
    )
    values.update(overrides)
    return AppConfig(**values)


def _request(version: str, *, statement: str = "合成题面 alpha beta") -> dict[str, Any]:
    return {
        "apiVersion": version,
        "requestId": "44444444-4444-4444-8444-444444444444",
        "contentHash": "4" * 64,
        "problem": {
            "title": "合成题目标题",
            "type": "traditional",
            "tagIds": ["math.basic"],
            "basicStatement": statement,
        },
    }


def _candidate(*, similarity: float = 0.95) -> dict[str, Any]:
    return {
        "source": "synthetic",
        "externalId": "synthetic/1",
        "title": "合成候选标题",
        "similarity": similarity,
    }


class _ScriptedBackend:
    def __init__(self, *outcomes: BackendSearchResult | BaseException) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0
        self.health_calls = 0

    def search(self, _query_text: str, _k: int) -> BackendSearchResult:
        self.calls += 1
        outcome = self._outcomes[(self.calls - 1) % len(self._outcomes)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def describe_health(self) -> dict[str, Any]:
        self.health_calls += 1
        return {"localStoreReady": True, "indexMetadataReady": True}


class _Harness:
    def __init__(self, service: AnklangService) -> None:
        handler_class = make_handler(service)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.last_raw = b""

    def request(
        self,
        method: str,
        path: str,
        body: bytes | str = b"",
        *,
        authorized: bool = True,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        if isinstance(body, str):
            body = body.encode("utf-8")
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
            self.last_raw = raw
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            resp_headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, payload, resp_headers
        finally:
            conn.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class V2ContractTests(unittest.TestCase):
    checked_at = "2026-08-01T00:00:00.000Z"

    def _build(
        self,
        *,
        status: str = "complete",
        reason: str = "complete",
        retryable: bool = False,
        retry_after: int | None = None,
        candidates: list[dict[str, Any]] | None = None,
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
            completion=completion,
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
            },
        )
        self.assertEqual(
            result["completion"],
            {"status": "complete", "reasonCode": "complete", "retryable": False},
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

    def test_cross_branch_constraints_reject_unsafe_results(self) -> None:
        invalid_builds = (
            lambda: self._build(
                status="unavailable",
                reason="service_unavailable",
                candidates=[_candidate()],
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

    def test_validator_rejects_extra_fields_at_every_v2_object_level(self) -> None:
        for mutate in (
            lambda value: value.update({"extra": True}),
            lambda value: value["completion"].update({"extra": True}),
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


class HttpV2Tests(unittest.TestCase):
    def _harness(
        self,
        backend: _ScriptedBackend,
        **config_overrides: Any,
    ) -> tuple[_Harness, AnklangService]:
        config = _config(**config_overrides)
        service = AnklangService(config, backend)
        harness = _Harness(service)
        self.addCleanup(harness.close)
        return harness, service

    def test_complete_v2_calculates_each_query(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([_candidate()]))
        harness, _ = self._harness(backend)

        first_status, first, first_headers = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        second_status, second, second_headers = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )

        self.assertEqual((first_status, second_status), (200, 200))
        # 没有缓存——每次都调用后端。
        self.assertEqual(backend.calls, 2)
        self.assertEqual(first["completion"], {
            "status": "complete",
            "reasonCode": "complete",
            "retryable": False,
        })
        self.assertEqual(first_headers.get("cache-control"), "no-store")
        self.assertEqual(second_headers.get("cache-control"), "no-store")

    def test_v1_keeps_old_success_shape(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([_candidate()]))
        harness, _ = self._harness(backend)

        v2_status, _, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        v1_status, v1, headers = harness.request(
            "POST", "/api/v1/checks/similarity", _request("1")
        )

        self.assertEqual((v2_status, v1_status), (200, 200))
        self.assertEqual(backend.calls, 2)
        self.assertEqual(
            set(v1),
            {"apiVersion", "contentHash", "checkedAt", "candidates"},
        )
        self.assertEqual(v1["apiVersion"], "1")
        self.assertNotIn("completion", v1)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_same_claimed_hash_with_different_statement_calls_backend_each_time(self) -> None:
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

    def test_partial_returns_ranked_candidates_without_policy_fields(self) -> None:
        backend = _ScriptedBackend(
            BackendSearchResult.partial([_candidate(similarity=0.99)])
        )
        harness, _ = self._harness(backend)
        for _ in range(2):
            status, payload, headers = harness.request(
                "POST", "/api/v2/checks/similarity", _request("2")
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["completion"]["status"], "partial")
            self.assertTrue(payload["candidates"])
            self.assertEqual(
                set(payload),
                {"apiVersion", "contentHash", "checkedAt", "completion", "candidates"},
            )
            self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(backend.calls, 2)

    def test_backend_extra_fields_do_not_escape_query_contract(self) -> None:
        backend = _ScriptedBackend(
            BackendSearchResult.partial(
                [{**_candidate(), "sameProblemSuggestion": True, "explanation": "不可出站"}]
            )
        )
        harness, _ = self._harness(backend)
        status, payload, _ = harness.request(
            "POST", "/api/v2/checks/similarity", _request("2")
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload["candidates"][0]),
            {"source", "externalId", "title", "similarity"},
        )

    def test_unavailable_v2_is_200_with_empty_candidates(self) -> None:
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
            self.assertEqual(payload["completion"]["reasonCode"], "search_rate_limited")
            self.assertEqual(payload["completion"]["retryAfterSeconds"], 23)
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
                    "message": "本次未能完成原题检索，请稍后重试。",
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

    def test_all_health_not_found_and_path_variant_responses_are_no_store(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([]))
        harness, _ = self._harness(backend)
        cases = (
            ("GET", "/api/v1/live", 200),
            ("GET", "/api/v1/health", 200),
            ("GET", "/unrelated", 404),
            ("POST", "/api/v2/checks/similarity?query=1", 404),
            ("POST", "/api/v2/checks/similarity/", 404),
        )
        for method, path, expected in cases:
            with self.subTest(method=method, path=path):
                status, _, headers = harness.request(method, path)
                self.assertEqual(status, expected)
                self.assertEqual(headers.get("cache-control"), "no-store")
                self.assertNotIn("Python", headers.get("server", ""))

    def test_live_is_fixed_local_and_never_calls_backend_health(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([]))
        harness, _ = self._harness(backend)
        status, payload, headers = harness.request("GET", "/api/v1/live")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {"status": "ok", "service": "anklang", "apiVersion": "1"},
        )
        self.assertEqual(backend.health_calls, 0)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_health_backend_exception_is_fixed_without_original_text(self) -> None:
        backend = _ScriptedBackend(BackendSearchResult([]))
        harness, _ = self._harness(backend)
        private_marker = "synthetic-private-health-detail"
        with patch.object(
            backend, "describe_health", side_effect=RuntimeError(private_marker)
        ):
            status, payload, headers = harness.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertNotIn(private_marker, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(headers.get("cache-control"), "no-store")


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
