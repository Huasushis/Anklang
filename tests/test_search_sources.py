from __future__ import annotations

import unittest

from anklang.backends import BackendSearchResult
from anklang.search_sources import (
    ConfiguredSearchBackend,
    SearchSourceConfig,
    SearchSourceRegistry,
)
from anklang.yuantiji import YuantijiSearchResult


class _LocalBackend:
    def __init__(self, result: BackendSearchResult) -> None:
        self.result = result
        self.calls = 0
        self.store = object()
        self.provider = object()

    @property
    def index_spec(self) -> None:
        return None

    def search(self, _query: str, _k: int) -> BackendSearchResult:
        self.calls += 1
        return self.result

    def close(self) -> None:
        return None

    def describe_health(self) -> dict[str, object]:
        return {"backend": "synthetic"}


class _Yuantiji:
    def __init__(self, candidates: list[dict[str, object]]) -> None:
        self.candidates = candidates
        self.calls = 0
        self.health_calls = 0

    def search(self, _query: str, _k: int, *, rerank: bool = False) -> YuantijiSearchResult:
        del rerank
        self.calls += 1
        return YuantijiSearchResult(self.candidates)

    def health(self) -> dict[str, object]:
        self.health_calls += 1
        return {"ok": True, "problems": 123}

    def cached_health(self) -> dict[str, object] | None:
        return None


def _candidate(source: str, external_id: str, similarity: float) -> dict[str, object]:
    return {
        "source": source,
        "externalId": external_id,
        "title": external_id,
        "similarity": similarity,
        "url": f"https://example.invalid/{external_id}",
        "statement": f"statement {external_id}",
    }


class SearchSourceTests(unittest.TestCase):
    def test_yuantiji_mode_does_not_call_local_index(self) -> None:
        local = _LocalBackend(BackendSearchResult([_candidate("local", "l", 0.9)]))
        registry = SearchSourceRegistry(
            SearchSourceConfig("yuantiji", "https://yuantiji.ac")
        )
        public = _Yuantiji([_candidate("codeforces", "p", 0.8)])
        registry._yuantiji = public  # type: ignore[attr-defined]
        backend = ConfiguredSearchBackend(local, registry)
        self.addCleanup(backend.close)

        result = backend.search("synthetic query", 5)

        self.assertEqual(result.status, "complete")
        self.assertEqual([item["externalId"] for item in result.candidates], ["p"])
        self.assertEqual(local.calls, 0)
        self.assertEqual(public.calls, 1)
        self.assertIn("statement", result.candidates[0])
        self.assertIn("url", result.candidates[0])

    def test_hybrid_returns_public_results_when_local_is_temporarily_unavailable(self) -> None:
        local = _LocalBackend(
            BackendSearchResult.unavailable(
                reason_code="search_backend_unavailable", retryable=True
            )
        )
        registry = SearchSourceRegistry(
            SearchSourceConfig("hybrid", "https://yuantiji.ac")
        )
        registry._yuantiji = _Yuantiji([_candidate("atcoder", "p", 0.7)])  # type: ignore[attr-defined]
        backend = ConfiguredSearchBackend(local, registry)
        self.addCleanup(backend.close)

        result = backend.search("synthetic query", 5)

        self.assertEqual(result.status, "partial")
        self.assertEqual([item["externalId"] for item in result.candidates], ["p"])

    def test_status_does_not_probe_public_service(self) -> None:
        local = _LocalBackend(BackendSearchResult([]))
        registry = SearchSourceRegistry(
            SearchSourceConfig("yuantiji", "https://yuantiji.ac")
        )
        public = _Yuantiji([])
        registry._yuantiji = public  # type: ignore[attr-defined]
        backend = ConfiguredSearchBackend(local, registry)
        self.addCleanup(backend.close)

        status = registry.status()

        self.assertEqual(status["mode"], "yuantiji")
        self.assertNotIn("yuantijiReady", status)
        self.assertEqual(public.health_calls, 0)
        self.assertEqual(public.calls, 0)

    def test_explicit_source_test_checks_health_and_a_synthetic_search(self) -> None:
        registry = SearchSourceRegistry(
            SearchSourceConfig("yuantiji", "https://yuantiji.ac")
        )
        public = _Yuantiji([])
        registry._yuantiji = public  # type: ignore[attr-defined]

        result = registry.test(
            SearchSourceConfig("yuantiji", "https://yuantiji.ac")
        )

        self.assertEqual(
            result,
            {"ok": True, "yuantijiReady": True, "yuantijiProblemCount": 123},
        )
        self.assertEqual(public.health_calls, 1)
        self.assertEqual(public.calls, 1)


if __name__ == "__main__":
    unittest.main()
