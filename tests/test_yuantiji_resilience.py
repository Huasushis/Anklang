"""yuantiji 上游的截断、有限重试、暂停调用和健康缓存测试。

全部网络响应和时间都由测试注入，不访问真实第三方服务，也不会真实等待。
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import unittest
import urllib.error
from typing import Any

from anklang.backends.reverse_proxy import ReverseProxyBackend
from anklang.cache import ResultCache
from anklang.config import AppConfig
from anklang.server import AnklangService
from anklang.yuantiji import YuantijiClient, YuantijiError


class _FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._body


class _ScriptedOpener:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        self.calls.append((request, timeout))
        if not self._outcomes:
            raise AssertionError("测试没有为这次上游调用准备响应。")
        outcome = self._outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(request, timeout)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, bytes):
            return _Response(outcome)
        return _Response(json.dumps(outcome).encode("utf-8"))


class _CapturingReviewClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"sameProblem": False}


class _DelayedLock:
    """测试用锁：用假时钟表示排队，不让测试真实等待。"""

    def __init__(
        self, fake_time: _FakeTime, acquire_after_seconds: float
    ) -> None:
        self._fake_time = fake_time
        self._acquire_after_seconds = acquire_after_seconds
        self.acquired = False
        self.acquire_timeouts: list[float] = []
        self.releases = 0

    def acquire(self, timeout: float) -> bool:
        self.acquire_timeouts.append(timeout)
        wait = min(timeout, self._acquire_after_seconds)
        if wait > 0:
            self._fake_time.sleep(wait)
        if self._acquire_after_seconds > timeout:
            return False
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            raise AssertionError("测试锁尚未取得，不能释放。")
        self.acquired = False
        self.releases += 1


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://yuantiji.test/api/search",
        status,
        "scripted",
        {},
        io.BytesIO(b""),
    )


def _client(
    opener: _ScriptedOpener,
    fake_time: _FakeTime,
    **overrides: Any,
) -> YuantijiClient:
    options = {
        "timeout_seconds": 12.0,
        "minimum_interval_seconds": 0.0,
        "max_retries": 0,
        "retry_base_delay_seconds": 0.5,
        "circuit_failure_threshold": 3,
        "circuit_open_seconds": 60.0,
        "health_cache_seconds": 60.0,
    }
    options.update(overrides)
    return YuantijiClient(
        "https://yuantiji.test",
        opener=opener,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
        **options,
    )


def _config(**overrides: Any) -> AppConfig:
    options = dict(
        port=8730,
        service_token=None,
        yuantiji_base_url="https://yuantiji.test",
        yuantiji_timeout_seconds=12.0,
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
        llm_model="test-model",
        llm_review_top_n=2,
        llm_timeout_seconds=10.0,
    )
    options.update(overrides)
    return AppConfig(**options)


class YuantijiResilienceTests(unittest.TestCase):
    def test_query_is_truncated_to_upstream_limit(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener([{"results": []}])
        client = _client(opener, fake_time)

        client.search("😀" * 16_001, k=8, rerank=False)

        request = opener.calls[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(len(payload["query"]), 16_000)
        self.assertEqual(payload["query"], "😀" * 16_000)

    def test_retry_is_bounded_and_uses_fake_time(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener([_http_error(503), {"results": []}])
        client = _client(opener, fake_time, max_retries=1)

        self.assertEqual(client.search("test", k=8, rerank=False), [])
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(fake_time.sleeps, [0.5])

    def test_queue_time_reduces_first_network_timeout(self) -> None:
        fake_time = _FakeTime()
        request_lock = _DelayedLock(fake_time, acquire_after_seconds=4.0)
        opener = _ScriptedOpener([{"results": []}])
        client = _client(
            opener,
            fake_time,
            request_lock=request_lock,
            search_budget_seconds=6.0,
        )

        self.assertEqual(client.search("test", k=8, rerank=False), [])
        self.assertEqual(opener.calls[0][1], 2.0)
        self.assertEqual(request_lock.releases, 1)
        self.assertEqual(fake_time.now, 104.0)

    def test_queue_cannot_consume_more_than_total_budget(self) -> None:
        fake_time = _FakeTime()
        request_lock = _DelayedLock(fake_time, acquire_after_seconds=10.0)
        opener = _ScriptedOpener([])
        client = _client(
            opener,
            fake_time,
            request_lock=request_lock,
            search_budget_seconds=4.0,
        )

        with self.assertRaises(YuantijiError):
            client.search("test", k=8, rerank=False)
        self.assertEqual(opener.calls, [])
        self.assertEqual(fake_time.now, 104.0)
        self.assertEqual(request_lock.releases, 0)

    def test_retry_uses_only_time_left_from_whole_search(self) -> None:
        fake_time = _FakeTime()

        def consume_timeout(_request: Any, timeout: float) -> Any:
            fake_time.sleep(timeout)
            raise TimeoutError()

        opener = _ScriptedOpener([consume_timeout, {"results": []}])
        client = _client(
            opener,
            fake_time,
            max_retries=1,
            retry_base_delay_seconds=1.0,
            search_budget_seconds=13.5,
        )

        self.assertEqual(client.search("test", k=8, rerank=False), [])
        self.assertEqual([call[1] for call in opener.calls], [12.0, 0.5])
        self.assertEqual(fake_time.sleeps, [12.0, 1.0])

    def test_retry_stops_when_waiting_uses_last_available_time(self) -> None:
        fake_time = _FakeTime()

        def consume_timeout(_request: Any, timeout: float) -> Any:
            fake_time.sleep(timeout)
            raise TimeoutError()

        opener = _ScriptedOpener([consume_timeout, {"results": []}])
        client = _client(
            opener,
            fake_time,
            max_retries=1,
            retry_base_delay_seconds=1.0,
            search_budget_seconds=13.0,
        )

        with self.assertRaises(YuantijiError):
            client.search("test", k=8, rerank=False)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(fake_time.now, 113.0)

    def test_request_interval_cannot_overrun_whole_search_budget(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener([{"results": []}])
        client = _client(
            opener,
            fake_time,
            minimum_interval_seconds=2.0,
            search_budget_seconds=1.0,
        )

        self.assertEqual(client.search("first", k=8, rerank=False), [])
        with self.assertRaises(YuantijiError):
            client.search("second", k=8, rerank=False)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(fake_time.now, 101.0)

    def test_response_finishing_after_budget_is_not_accepted(self) -> None:
        fake_time = _FakeTime()

        def return_too_late(_request: Any, timeout: float) -> Any:
            fake_time.sleep(timeout + 0.25)
            return {"results": []}

        opener = _ScriptedOpener([return_too_late])
        client = _client(
            opener,
            fake_time,
            search_budget_seconds=2.0,
        )

        with self.assertRaises(YuantijiError):
            client.search("test", k=8, rerank=False)
        self.assertEqual(opener.calls[0][1], 2.0)
        self.assertEqual(fake_time.now, 102.25)

    def test_invalid_json_is_not_retried(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener([b"not json", {"results": []}])
        client = _client(opener, fake_time, max_retries=1)

        with self.assertRaises(YuantijiError):
            client.search("test", k=8, rerank=False)
        self.assertEqual(len(opener.calls), 1)

    def test_interrupted_response_is_retried_within_limit(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener(
            [http.client.IncompleteRead(b"partial", 10), {"results": []}]
        )
        client = _client(opener, fake_time, max_retries=1)

        self.assertEqual(client.search("test", k=8, rerank=False), [])
        self.assertEqual(len(opener.calls), 2)

    def test_huge_json_number_and_score_overflow_are_safe_failures(self) -> None:
        fake_time = _FakeTime()
        huge_json = (
            b'{"results":[{"uid":"x","title":"t","src":"s","cos":'
            + (b"9" * 5_000)
            + b"}]}"
        )
        opener = _ScriptedOpener([huge_json])
        client = _client(opener, fake_time)

        with self.assertRaises(YuantijiError):
            client.search("test", k=8, rerank=False)
        self.assertIsNone(
            YuantijiClient._map_candidate(
                {
                    "uid": "x",
                    "title": "t",
                    "src": "s",
                    "cos": 10**400,
                }
            )
        )

    def test_circuit_pauses_calls_and_recovers_after_cooldown(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener(
            [TimeoutError(), TimeoutError(), {"results": []}]
        )
        client = _client(
            opener,
            fake_time,
            circuit_failure_threshold=2,
            circuit_open_seconds=60.0,
        )

        with self.assertRaises(YuantijiError):
            client.search("one", k=8, rerank=False)
        with self.assertRaises(YuantijiError):
            client.search("two", k=8, rerank=False)
        with self.assertRaises(YuantijiError):
            client.search("three", k=8, rerank=False)
        self.assertEqual(len(opener.calls), 2)

        fake_time.now += 61.0
        self.assertEqual(client.search("four", k=8, rerank=False), [])
        self.assertEqual(len(opener.calls), 3)

    def test_health_success_and_failure_are_cached(self) -> None:
        fake_time = _FakeTime()
        success_opener = _ScriptedOpener(
            [{"ok": True, "problems": 3}, {"ok": True, "problems": 4}]
        )
        success_client = _client(success_opener, fake_time)
        self.assertTrue(success_client.health()["ok"])
        self.assertTrue(success_client.health()["ok"])
        self.assertEqual(len(success_opener.calls), 1)
        fake_time.now += 61.0
        self.assertEqual(success_client.health()["problems"], 4)
        self.assertEqual(len(success_opener.calls), 2)

        failure_time = _FakeTime()
        failure_opener = _ScriptedOpener([TimeoutError(), {"ok": True}])
        failure_client = _client(failure_opener, failure_time)
        self.assertFalse(failure_client.health()["ok"])
        self.assertFalse(failure_client.health()["ok"])
        self.assertEqual(len(failure_opener.calls), 1)

    def test_health_refresh_wait_cannot_overrun_whole_budget(self) -> None:
        fake_time = _FakeTime()
        health_refresh_lock = _DelayedLock(
            fake_time, acquire_after_seconds=10.0
        )
        request_lock = _DelayedLock(fake_time, acquire_after_seconds=0.0)
        opener = _ScriptedOpener([])
        client = _client(
            opener,
            fake_time,
            health_refresh_lock=health_refresh_lock,
            request_lock=request_lock,
            search_budget_seconds=4.0,
        )

        self.assertEqual(client.health(), {"ok": False})
        self.assertEqual(fake_time.now, 104.0)
        self.assertEqual(health_refresh_lock.acquire_timeouts, [4.0])
        self.assertEqual(health_refresh_lock.releases, 0)
        self.assertEqual(request_lock.acquire_timeouts, [])
        self.assertEqual(request_lock.releases, 0)
        self.assertEqual(opener.calls, [])

    def test_health_refresh_releases_locks_it_acquired(self) -> None:
        fake_time = _FakeTime()
        health_refresh_lock = _DelayedLock(
            fake_time, acquire_after_seconds=0.0
        )
        request_lock = _DelayedLock(fake_time, acquire_after_seconds=0.0)
        opener = _ScriptedOpener([{"ok": True}])
        client = _client(
            opener,
            fake_time,
            health_refresh_lock=health_refresh_lock,
            request_lock=request_lock,
        )

        self.assertEqual(client.health(), {"ok": True})
        self.assertEqual(health_refresh_lock.releases, 1)
        self.assertEqual(request_lock.releases, 1)
        self.assertEqual(len(opener.calls), 1)

    def test_health_result_does_not_change_search_failure_count(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener(
            [TimeoutError(), {"ok": True}, TimeoutError()]
        )
        client = _client(
            opener,
            fake_time,
            circuit_failure_threshold=2,
        )

        with self.assertRaises(YuantijiError):
            client.search("one", k=8, rerank=False)
        self.assertTrue(client.health()["ok"])
        with self.assertRaises(YuantijiError):
            client.search("two", k=8, rerank=False)
        with self.assertRaises(YuantijiError):
            client.search("three", k=8, rerank=False)
        self.assertEqual(len(opener.calls), 3)

    def test_health_failure_does_not_block_search(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener([TimeoutError(), {"results": []}])
        client = _client(
            opener,
            fake_time,
            circuit_failure_threshold=1,
        )

        self.assertFalse(client.health()["ok"])
        self.assertEqual(client.search("test", k=8, rerank=False), [])
        self.assertEqual(len(opener.calls), 2)

    def test_health_can_probe_again_when_search_pause_expires(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener([TimeoutError(), {"ok": True}])
        client = _client(
            opener,
            fake_time,
            circuit_failure_threshold=1,
            circuit_open_seconds=10.0,
            health_cache_seconds=60.0,
        )

        with self.assertRaises(YuantijiError):
            client.search("test", k=8, rerank=False)
        self.assertFalse(client.health()["ok"])
        self.assertEqual(len(opener.calls), 1)
        fake_time.now += 11.0
        self.assertTrue(client.health()["ok"])
        self.assertEqual(len(opener.calls), 2)

    def test_mixed_candidates_skip_malformed_items(self) -> None:
        fake_time = _FakeTime()
        opener = _ScriptedOpener(
            [
                {
                    "results": [
                        {"title": "missing id", "cos": 0.9},
                        {
                            "uid": "example/1",
                            "title": "valid",
                            "src": "example",
                            "cos": 0.8,
                        },
                    ]
                }
            ]
        )
        client = _client(opener, fake_time)
        candidates = client.search("test", k=8, rerank=False)
        self.assertEqual([item["externalId"] for item in candidates], ["example/1"])

    def test_candidate_text_limits_use_javascript_string_length(self) -> None:
        exact = {
            "uid": "😀" * 100,
            "title": "😀" * 100,
            "src": "😀" * 40,
            "cos": 0.8,
        }
        candidate = YuantijiClient._map_candidate(exact)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["externalId"], exact["uid"])
        self.assertEqual(candidate["title"], exact["title"])
        self.assertEqual(candidate["source"], exact["src"])

        for field, value in (
            ("uid", ("😀" * 100) + "x"),
            ("title", ("😀" * 100) + "x"),
            ("src", ("😀" * 40) + "x"),
        ):
            with self.subTest(field=field):
                oversized = dict(exact)
                oversized[field] = value
                self.assertIsNone(YuantijiClient._map_candidate(oversized))

    def test_oversized_candidate_never_reaches_review_model_or_cache(self) -> None:
        fake_time = _FakeTime()
        private_marker = "UPSTREAM_PRIVATE_TITLE_MUST_NOT_LEAK"
        oversized_result = {
            "results": [
                {
                    "uid": "problem-1",
                    "title": private_marker + ("x" * 500_000),
                    "src": "public-source",
                    "cos": 0.99,
                    "original": "private candidate statement",
                }
            ]
        }
        opener = _ScriptedOpener([oversized_result, oversized_result])
        client = _client(opener, fake_time)
        backend = ReverseProxyBackend(client, use_rerank=False)
        config = _config(llm_review_enabled=True)
        review_client = _CapturingReviewClient()
        service = AnklangService(
            config,
            backend,
            ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
            review_client,  # type: ignore[arg-type]
        )
        request = {
            "content_hash": "c" * 64,
            "basic_statement": "private submitted statement",
            "title": "synthetic",
            "type": "traditional",
            "tag_ids": ["test"],
        }

        first_result = service.check_similarity(request)
        second_result = service.check_similarity(request)

        for result in (first_result, second_result):
            self.assertEqual(result["apiVersion"], "1")
            self.assertEqual(result["candidates"], [])
            self.assertFalse(result["recommendation"]["blockSubmission"])
            self.assertNotIn(
                private_marker,
                json.dumps(result, ensure_ascii=False),
            )
        self.assertEqual(review_client.calls, [])
        self.assertEqual(len(opener.calls), 2)

    def test_timeout_http_error_and_malformed_response_degrade_safely(self) -> None:
        scenarios = (
            [TimeoutError()],
            [_http_error(500)],
            [b"not json"],
            [
                b'{"results":[{"uid":"x","title":"t","src":"s","cos":'
                + (b"9" * 5_000)
                + b"}]}"
            ],
            [{"unexpected": []}],
        )
        for outcomes in scenarios:
            with self.subTest(outcome_type=type(outcomes[0]).__name__):
                fake_time = _FakeTime()
                opener = _ScriptedOpener(outcomes)
                client = _client(opener, fake_time)
                backend = ReverseProxyBackend(client, use_rerank=False)
                config = _config()
                service = AnklangService(
                    config,
                    backend,
                    ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
                    None,
                )
                result = service.check_similarity(
                    {
                        "content_hash": "a" * 64,
                        "basic_statement": "synthetic statement",
                        "title": "synthetic",
                        "type": "traditional",
                        "tag_ids": ["test"],
                    }
                )
                self.assertEqual(result["apiVersion"], "1")
                self.assertEqual(result["contentHash"], "a" * 64)
                self.assertEqual(result["candidates"], [])
                self.assertFalse(result["recommendation"]["blockSubmission"])
                self.assertNotIn(
                    "scripted",
                    json.dumps(result, ensure_ascii=False),
                )

    def test_deeply_nested_json_degrades_without_cache_or_content_leak(self) -> None:
        fake_time = _FakeTime()
        upstream_private_text = "测试用外部原文与内部状态，不得进入结果。"
        encoded_private_text = json.dumps(
            upstream_private_text, ensure_ascii=False
        ).encode("utf-8")
        nesting = 2_000
        deeply_nested_json = (
            b'{"results":'
            + (b"[" * nesting)
            + encoded_private_text
            + (b"]" * nesting)
            + b"}"
        )
        opener = _ScriptedOpener([deeply_nested_json, deeply_nested_json])
        client = _client(opener, fake_time)
        backend = ReverseProxyBackend(client, use_rerank=False)
        config = _config()
        service = AnklangService(
            config,
            backend,
            ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
            None,
        )
        submitted_statement = "测试用未公开题面，不得进入响应或错误输出。"
        request = {
            "content_hash": "b" * 64,
            "basic_statement": submitted_statement,
            "title": "synthetic",
            "type": "traditional",
            "tag_ids": ["test"],
        }

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            first_result = service.check_similarity(request)
            second_result = service.check_similarity(request)

        for result in (first_result, second_result):
            self.assertEqual(result["apiVersion"], "1")
            self.assertEqual(result["contentHash"], "b" * 64)
            self.assertEqual(result["candidates"], [])
            self.assertFalse(result["recommendation"]["blockSubmission"])
            serialized_result = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(submitted_statement, serialized_result)
            self.assertNotIn(upstream_private_text, serialized_result)
            self.assertNotIn("RecursionError", serialized_result)
            self.assertNotIn("YuantijiError", serialized_result)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
