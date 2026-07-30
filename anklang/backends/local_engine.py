"""阶段 2 本地检索后端：对自建题库做"向量召回 + 关键词召回"的混合检索。

设计对应 docs/plan.md 3.5 节的结论："分别取向量召回的 Top-K1 和关键词召回的
Top-K2，取并集去重后，统一交给 LLM 复核环节做最终判断，不要自己再设计一套复杂的
分数融合公式"——这里的合并策略就是"同一候选取两路分数中较高的一个"，不做加权求和
之类更复杂的融合，融合之后的候选和阶段 1 一样交给 anklang.review.evaluate 判定。

- 向量召回：把待查题面也算一次 embedding，和题库里每道已经算过向量的题目计算余弦
  相似度（"向量检索"：把文字转换成一串数字组成的"向量"，越相似的文字向量在数学上
  越"靠近"，通过比较向量距离找出相似题目）。
- 关键词召回：没有依赖 SQLite 的 FTS5 全文检索扩展（部署环境的 SQLite 是否编译了
  FTS5 不确定，为了不引入不确定的依赖，这里改用一个不需要任何扩展、纯 Python 的
  字符/词粒度重合度打分，见本文件末尾的 _keyword_score），足以捕捉"整段题面几乎
  逐字照抄"这种字面高度重合的情况——这正是 docs/plan.md 3.5 节里关键词检索要
  补充覆盖的场景。

题库为空、或没配置 embedding 服务时这个后端都能正常工作（分别表现为搜不到东西、
只做关键词召回），不会因此报错——与阶段 1 的"优雅降级"设计取向一致，这也是它可以
在没有真实 embedding 凭据的环境里先跑起来、之后再补 embedding 的原因。
"""
from __future__ import annotations

import math
import re
from typing import Any

from . import BackendError, BackendSearchResult
from ..embedding import EmbeddingClient, EmbeddingError
from ..store import ProblemStore, StoredProblem
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
    ) -> None:
        self.store = store
        self.embedder = embedder
        self._vector_top_k = vector_top_k
        self._keyword_top_k = keyword_top_k

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        try:
            problems = self.store.iter_all()
        except Exception as error:
            # 存储层读取失败时不让整个请求崩溃，转换成 BackendError 交给 server
            # 降级处理，和阶段 1 反代后端遇到 YuantijiError 时的处理方式保持一致。
            raise BackendError("本地题库读取失败。") from error

        query_vector: list[float] | None = None
        degraded = False
        if self.embedder is not None:
            try:
                query_vector = self.embedder.embed_one(query_text)
            except EmbeddingError:
                # 已配置的文字转数字服务调用失败时仍做关键词检索，但要让服务层知道
                # 结果不完整，从而不缓存；未配置服务本来就是正常的关键词模式。
                degraded = True

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
        return BackendSearchResult(candidates=ranked[:k], degraded=degraded)

    def describe_health(self) -> dict[str, Any]:
        try:
            count = self.store.count()
        except Exception:
            # 健康检查本身不应该因为存储异常而失败，这里故意宽泛捕获，转换成状态字段。
            return {
                "localProblemCount": None,
                "embeddingAvailable": self.embedder is not None,
                "localStoreReady": False,
            }
        return {
            "localProblemCount": count,
            "embeddingAvailable": self.embedder is not None,
            "localStoreReady": True,
        }


def _to_candidate(problem: StoredProblem, similarity: float) -> dict[str, Any]:
    snippet = " ".join(problem.statement.split())[:400]
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
        "explanation": "该候选由本地题库的文字含义或字面重合信号找到，请人工核对来源记录。",
    }
    if problem.url:
        candidate["url"] = problem.url
    if snippet:
        # 仅供当前请求里的可选模型复核使用。contracts.build_result 不会把这个
        # 内部字段写进返回结果，ResultCache 也只缓存已经清理过的契约结果。
        candidate["_reviewExcerpt"] = snippet
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
