"""本地检索后端：对本地题库做"向量召回 + 关键词召回"的混合检索。

向量召回把待查题面也转换成 embedding，和题库里每道已经算过向量的题目计算余弦相似度；
关键词召回用纯 Python 的字符/词粒度重合度打分，覆盖"整段题面几乎逐字照抄"的情况。
两路结果取并集去重，同一候选取两路分数中较高的一个。

没配置 embedding 服务时，只要目标关键词索引身份完整，就会正常完成关键词召回；空题库、
索引身份不完整或已配置的 embedding 调用失败会明确返回不可用或部分完成，不能把未检索到
误报为"没有候选"。

Anklang 是 is-my-problem-new（MIT，Copyright (c) 2023 Ziqian Zhong）的最小直接改编。
"""
from __future__ import annotations

import math
import re
from typing import Any

from . import BackendError, BackendSearchResult
from ..embedding import EmbeddingClient, EmbeddingError
from ..store import EmbeddingIndexSpec, ProblemStore, StoredProblem
from ..vectormath import cosine_similarity

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")


class LocalEngineBackend:
    """本地题库的向量 + 关键词混合检索后端。"""

    def __init__(
        self,
        store: ProblemStore,
        embedder: EmbeddingClient | None,
        vector_top_k: int = 20,
        keyword_top_k: int = 20,
        index_spec: EmbeddingIndexSpec | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        if embedder is None:
            if index_spec is not None:
                raise ValueError("未配置向量客户端时不能提供索引规格。")
            self.index_spec = None
        else:
            self.index_spec = index_spec or EmbeddingIndexSpec(
                model=embedder.model,
                dimensions=embedder.dimensions,
            )
        self._vector_top_k = vector_top_k
        self._keyword_top_k = keyword_top_k

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        try:
            snapshot = self.store.search_snapshot(self.index_spec)
        except Exception as error:
            # 存储层读取失败时不让整个请求崩溃，转换成稳定的后端错误。
            raise BackendError("本地题库读取失败。") from error

        problems = snapshot.problems
        complete = snapshot.vector_status in {"ready", "disabled"}
        partial_retryable = snapshot.vector_status in {
            "incomplete_vectors",
            "verification_required",
        }
        if not problems:
            if complete:
                return BackendSearchResult(
                    candidates=[],
                )
            return BackendSearchResult.unavailable(
                reason_code="search_backend_unavailable",
                retryable=False,
            )

        query_vector: list[float] | None = None
        if self.embedder is not None and snapshot.vector_ready:
            try:
                query_vector = self.embedder.embed_one(query_text)
            except EmbeddingError:
                # 已配置的文字转数字服务调用失败时仍做关键词检索，但要让服务层知道
                # 结果不完整；未配置服务本来就是正常的关键词模式。
                complete = False
                partial_retryable = True

        query_tokens = _tokenize(query_text)

        vector_scored: list[tuple[float, StoredProblem]] = []
        keyword_scored: list[tuple[float, StoredProblem]] = []
        for problem in problems:
            if query_vector is not None and problem.embedding is not None:
                similarity = cosine_similarity(query_vector, problem.embedding)
                # 无法安全比较的向量会得到 0 分；不要把这种记录塞进 Top-K，
                # 否则题库很小时它仍可能作为候选显示。
                if similarity > 0.0:
                    vector_scored.append((similarity, problem))
            if query_tokens:
                score = _keyword_score(query_tokens, problem.statement)
                if score > 0.0:
                    keyword_scored.append((score, problem))

        vector_scored.sort(key=lambda pair: pair[0], reverse=True)
        keyword_scored.sort(key=lambda pair: pair[0], reverse=True)

        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for similarity, problem in vector_scored[: self._vector_top_k]:
            merged[(problem.source, problem.external_id)] = _to_candidate(problem, similarity)
        for score, problem in keyword_scored[: self._keyword_top_k]:
            key = (problem.source, problem.external_id)
            if key in merged:
                merged[key]["similarity"] = max(merged[key]["similarity"], score)
            else:
                merged[key] = _to_candidate(problem, score)

        ranked = sorted(merged.values(), key=lambda item: item["similarity"], reverse=True)
        candidates = ranked[:k]
        if complete:
            return BackendSearchResult(
                candidates=candidates,
            )
        return BackendSearchResult.partial(
            candidates,
            reason_code="search_partial",
            retryable=partial_retryable,
        )

    def describe_health(self) -> dict[str, Any]:
        try:
            inspection = self.store.inspect_index(self.index_spec)
        except Exception:
            # 健康检查本身不应该因为存储异常而失败，这里故意宽泛捕获，转换成状态字段。
            return {
                "localProblemCount": None,
                "embeddingAvailable": self.embedder is not None,
                "localStoreReady": False,
                "indexMetadataReady": False,
                "vectorIndexReady": False,
                "vectorIndexStatus": "store_error",
            }
        return {
            "localProblemCount": inspection.problem_count,
            "embeddingAvailable": self.embedder is not None,
            "localStoreReady": True,
            "indexMetadataReady": inspection.metadata_ready,
            "vectorIndexReady": inspection.vector_ready,
            "vectorIndexStatus": inspection.status,
        }


def _to_candidate(problem: StoredProblem, similarity: float) -> dict[str, Any]:
    if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
        numeric_similarity = 0.0
    else:
        numeric_similarity = float(similarity)
        if not math.isfinite(numeric_similarity):
            numeric_similarity = 0.0
    candidate: dict[str, Any] = {
        "source": problem.source,
        "externalId": problem.external_id,
        "title": problem.title,
        "similarity": max(0.0, min(1.0, numeric_similarity)),
    }
    if problem.url:
        candidate["url"] = problem.url
    return candidate


def _tokenize(text: str) -> set[str]:
    """把文本切成一个粗粒度的 token 集合：英文/数字按连续片段切词，中文按单字切词。
    这不是严谨的中文分词（没有处理词语边界，比如不知道"数组"是一个词），但作为
    "字面重合度"的启发式已经够用——近乎逐字照抄的题面，字符级重合度依然会很高，
    这正是关键词召回要捕捉的场景（补充向量召回，而不是取代它）。
    """
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(text)}


def _keyword_score(query_tokens: set[str], statement: str) -> float:
    """关键词重合度：待查文本的 token 有多大比例在候选题面里出现过，范围 [0, 1]。"""
    statement_tokens = _tokenize(statement)
    if not query_tokens or not statement_tokens:
        return 0.0
    overlap = query_tokens & statement_tokens
    return len(overlap) / len(query_tokens)
