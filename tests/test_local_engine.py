"""LocalEngineBackend 的单元测试。

题库里的向量都是直接构造好写入 store 的（模拟"已经算过 embedding 的历史题目"）；
真正会调用 embedding 客户端的只有"给待查题面算一次向量"这一步，这里用注入的假
urllib opener 拦截，按文本查表返回预先设计好的向量，不发起真实网络调用、不依赖
真实的语义质量——只用来验证 LocalEngineBackend 自己的排序、合并、降级逻辑对不对。
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from anklang.backends.local_engine import LocalEngineBackend
from anklang.contracts import build_result
from anklang.embedding import EmbeddingClient
from anklang.store import EmbeddingIndexSpec, ProblemStore

# 四个方向清晰分离的向量，避免真实语义向量里"常见字/词"造成的噪音，
# 让"谁更相似"这件事在测试里是无歧义的。
_ARRAY_SUM_VECTOR = [1.0, 0.0, 0.0, 0.0]
_ARRAY_SUM_2_VECTOR = [0.95, 0.1, 0.0, 0.0]
_LIS_VECTOR = [0.0, 1.0, 0.0, 0.0]
_GRAPH_VECTOR = [0.0, 0.0, 1.0, 0.0]

_QUERY_TEXT = "给定 n 个整数，求它们的和"
_QUERY_VECTOR = [0.97, 0.05, 0.0, 0.0]  # 刻意设计成明显更接近"数组求和"类的向量
_INDEX_SPEC = EmbeddingIndexSpec("text-embedding-v4", 4)


class _FakeEmbeddingOpener:
    """模拟 urllib.request.urlopen：只认识测试里注册过的查询文本，按文本查表返回
    预设向量；遇到没注册的文本会抛 KeyError（说明测试用例本身写错了）。"""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = vectors_by_text
        self.calls = 0

    def __call__(self, request: Any, timeout: float) -> Any:  # noqa: ARG002
        self.calls += 1
        body = json.loads(request.data.decode("utf-8"))
        raw_input = body["input"]
        texts = raw_input if isinstance(raw_input, list) else [raw_input]
        vectors = [self._vectors_by_text[text] for text in texts]
        payload = {
            "model": "text-embedding-v4",
            "data": [{"embedding": vector} for vector in vectors],
        }
        response_body = json.dumps(payload).encode("utf-8")

        class _Response:
            def __enter__(self_inner) -> "_Response":
                return self_inner

            def __exit__(self_inner, *_args: Any) -> None:
                return None

            def read(self_inner, _limit: int) -> bytes:
                return response_body

        return _Response()


def _make_embedder(vectors_by_text: dict[str, list[float]]) -> tuple[EmbeddingClient, _FakeEmbeddingOpener]:
    opener = _FakeEmbeddingOpener(vectors_by_text)
    client = EmbeddingClient(
        base_url="https://dashscope.test/compatible-mode/v1",
        api_key="test-key",
        model="text-embedding-v4",
        dimensions=4,
        opener=opener,
    )
    return client, opener


class LocalEngineBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.addCleanup(self.store.close)
        self.embedder, self.opener = _make_embedder({_QUERY_TEXT: _QUERY_VECTOR})
        self.store.prepare_embedding_writes(_INDEX_SPEC)

        self.store.add_problem(
            source="unit-test",
            external_id="array-sum",
            title="数组求和",
            statement="给定 n 个整数，输出它们的和。",
            content_hash="h1",
            embedding=_ARRAY_SUM_VECTOR,
            index_spec=_INDEX_SPEC,
        )
        self.store.add_problem(
            source="unit-test",
            external_id="array-sum-2",
            title="数组求和的变体",
            statement="给定 n 个整数，计算它们的总和并输出。",
            content_hash="h2",
            embedding=_ARRAY_SUM_2_VECTOR,
            index_spec=_INDEX_SPEC,
        )
        self.store.add_problem(
            source="unit-test",
            external_id="lis",
            title="最长上升子序列",
            statement="给定一个序列，求最长严格递增子序列的长度。",
            content_hash="h3",
            embedding=_LIS_VECTOR,
            index_spec=_INDEX_SPEC,
        )
        self.store.add_problem(
            source="unit-test",
            external_id="graph",
            title="最短路径",
            statement="给定一张带权无向图，求最短路径长度。",
            content_hash="h4",
            embedding=_GRAPH_VECTOR,
            index_spec=_INDEX_SPEC,
        )

    def test_similar_problem_ranks_first(self) -> None:
        backend = LocalEngineBackend(self.store, self.embedder, vector_top_k=10, keyword_top_k=10)
        search_result = backend.search(_QUERY_TEXT, k=5)
        results = search_result.candidates

        self.assertTrue(results)
        self.assertEqual(search_result.status, "complete")
        self.assertEqual(results[0]["externalId"], "array-sum")
        ranked_ids = [item["externalId"] for item in results]
        self.assertLess(ranked_ids.index("array-sum-2"), ranked_ids.index("graph"))
        self.assertLess(ranked_ids.index("array-sum-2"), ranked_ids.index("lis"))
        # 只对查询文本算了一次向量，题库里的向量是直接写入的，不应该重复调用 embedding。
        self.assertEqual(self.opener.calls, 1)

    def test_candidate_shape_matches_contract_expectations(self) -> None:
        backend = LocalEngineBackend(self.store, self.embedder, vector_top_k=10, keyword_top_k=10)
        search_result = backend.search(_QUERY_TEXT, k=5)
        results = search_result.candidates
        top = results[0]
        self.assertEqual(top["source"], "unit-test")
        self.assertIn("similarity", top)
        self.assertTrue(0.0 <= top["similarity"] <= 1.0)
        self.assertTrue(top["url"].startswith("http") if "url" in top else True)
        self.assertIn("_reviewExcerpt", top)

        result = build_result("a" * 64, [top], False, "已完成")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("_reviewExcerpt", result["candidates"][0])
        self.assertNotIn("degraded", serialized)
        self.assertNotIn("输出它们的和", serialized)

    def test_falls_back_to_keyword_when_embedding_unavailable(self) -> None:
        backend = LocalEngineBackend(self.store, embedder=None, vector_top_k=10, keyword_top_k=10)
        search_result = backend.search("数组求和 给定 n 个整数 输出它们的和", k=5)
        results = search_result.candidates
        self.assertTrue(results)
        self.assertEqual(search_result.status, "complete")
        self.assertEqual(self.opener.calls, 0)

    def test_embedding_failure_marks_keyword_fallback_as_degraded(self) -> None:
        # 注册的表里没有这个 query 文本，_FakeEmbeddingOpener 会抛 KeyError，
        # EmbeddingClient 应该把它当成失败，LocalEngineBackend 捕获后降级为关键词召回。
        broken_embedder, opener = _make_embedder({})
        backend = LocalEngineBackend(self.store, broken_embedder, vector_top_k=10, keyword_top_k=10)
        search_result = backend.search("数组求和 给定 n 个整数 输出它们的和", k=5)
        results = search_result.candidates
        self.assertTrue(results)
        self.assertEqual(search_result.status, "partial")
        self.assertEqual(search_result.reason_code, "search_partial")
        self.assertTrue(search_result.retryable)
        self.assertEqual(opener.calls, 1)

    def test_respects_k_limit(self) -> None:
        backend = LocalEngineBackend(self.store, self.embedder, vector_top_k=10, keyword_top_k=10)
        results = backend.search(_QUERY_TEXT, k=2).candidates
        self.assertLessEqual(len(results), 2)

    def test_empty_store_returns_empty(self) -> None:
        empty_store = ProblemStore(":memory:")
        self.addCleanup(empty_store.close)
        backend = LocalEngineBackend(empty_store, self.embedder)
        search_result = backend.search(_QUERY_TEXT, k=5)
        self.assertEqual(search_result.candidates, [])
        self.assertEqual(search_result.status, "unavailable")
        self.assertEqual(self.opener.calls, 0)

    def test_describe_health_reports_count_and_embedding_availability(self) -> None:
        backend = LocalEngineBackend(self.store, self.embedder)
        info = backend.describe_health()
        self.assertEqual(info["localProblemCount"], 4)
        self.assertTrue(info["embeddingAvailable"])
        self.assertTrue(info["localStoreReady"])
        self.assertTrue(info["indexMetadataReady"])

        backend_no_embed = LocalEngineBackend(self.store, embedder=None)
        self.assertFalse(backend_no_embed.describe_health()["embeddingAvailable"])

    def test_missing_metadata_uses_keyword_without_calling_embedder(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        store.add_problem(
            source="unit-test",
            external_id="keyword-only",
            title="关键词候选",
            statement="数组 求和 关键词候选",
            content_hash="keyword-hash",
        )
        embedder, opener = _make_embedder({})
        backend = LocalEngineBackend(store, embedder)

        result = backend.search("数组 求和", k=5)

        self.assertEqual(result.status, "partial")
        self.assertFalse(result.retryable)
        self.assertEqual(result.candidates[0]["externalId"], "keyword-only")
        self.assertEqual(opener.calls, 0)
        health = backend.describe_health()
        self.assertFalse(health["indexMetadataReady"])
        self.assertFalse(health["vectorIndexReady"])
        self.assertEqual(health["vectorIndexStatus"], "uninitialized")
        self.assertNotIn("model", json.dumps(health))

    def test_same_dimension_other_model_disables_all_vector_scores(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        stored_spec = EmbeddingIndexSpec("different-model", 4)
        store.prepare_embedding_writes(stored_spec)
        store.add_problem(
            source="unit-test",
            external_id="keyword-only",
            title="关键词候选",
            statement="数组 求和 关键词候选",
            content_hash="keyword-hash",
            embedding=[1.0, 0.0, 0.0, 0.0],
            index_spec=stored_spec,
        )
        embedder, opener = _make_embedder({})
        backend = LocalEngineBackend(store, embedder)

        result = backend.search("数组 求和", k=5)

        self.assertEqual(result.status, "partial")
        self.assertFalse(result.retryable)
        self.assertEqual(result.candidates[0]["externalId"], "keyword-only")
        self.assertEqual(opener.calls, 0)


if __name__ == "__main__":
    unittest.main()
