"""查重结果的判定：整理候选、按阈值决定是否建议拦截，可选调用 LLM 复核。

流程：
1. 过滤掉相似度低于显示下限的候选，按相似度降序；
2. 若开启 LLM 复核且最高相似度达到复核线，用 LLM 判断前 N 组是否同题，
   结果写进对应候选的 sameProblemSuggestion / explanation，并可提升拦截建议；
3. 拦截建议：最高相似度超过 block_threshold，或 LLM 确认存在同题，即建议不要提交。

LLM 复核失败不影响主流程：降级为“仅按相似度阈值判定”，不因外部模型不可用而报错。
"""
from __future__ import annotations

import json
from typing import Any

from .config import AppConfig
from .llm import LlmClient, LlmError


def evaluate(
    config: AppConfig,
    request: dict[str, Any],
    raw_candidates: list[dict[str, Any]],
    llm_client: LlmClient | None,
) -> dict[str, Any]:
    visible = [
        candidate
        for candidate in raw_candidates
        if candidate["similarity"] >= config.minimum_similarity
    ]
    visible.sort(key=lambda candidate: candidate["similarity"], reverse=True)

    highest = visible[0]["similarity"] if visible else 0.0
    llm_confirms_same = False

    if (
        config.llm_review_enabled
        and llm_client is not None
        and visible
        and highest >= config.minimum_similarity
    ):
        for candidate in visible[: config.llm_review_top_n]:
            verdict = _llm_review_one(llm_client, config, request, candidate)
            if verdict is None:
                continue
            candidate["sameProblemSuggestion"] = verdict["sameProblem"]
            if verdict.get("explanation"):
                candidate["explanation"] = verdict["explanation"]
            if verdict["sameProblem"]:
                llm_confirms_same = True

    block = highest >= config.block_threshold or llm_confirms_same
    message = _summarize(visible, highest, block, llm_confirms_same)
    return {
        "candidates": visible,
        "block_submission": block,
        "message": message,
    }


def _llm_review_one(
    llm_client: LlmClient,
    config: AppConfig,
    request: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    system = (
        "你是算法竞赛命题查重助手。给你一道待投稿题目的题面和一道候选公开题目的信息，"
        "判断它们是否是同一道题（考点、输入输出、数据范围本质一致即算同题，仅背景包装不同也算）。"
        "只输出 JSON：{\"sameProblem\": true|false, \"explanation\": \"简短中文理由\"}。"
    )
    user = json.dumps(
        {
            "submitted": {"title": request["title"], "statement": request["basic_statement"][:6000]},
            "candidate": {
                "title": candidate["title"],
                "source": candidate["source"],
                "similarity": candidate["similarity"],
                "excerpt": (candidate.get("explanation") or "")[:2000],
            },
        },
        ensure_ascii=False,
    )
    try:
        data = llm_client.complete_json(
            model=config.llm_model,
            system=system,
            user=user,
            timeout_seconds=config.llm_timeout_seconds,
        )
    except LlmError:
        return None
    same = data.get("sameProblem")
    if not isinstance(same, bool):
        return None
    explanation = data.get("explanation")
    return {
        "sameProblem": same,
        "explanation": explanation.strip()[:2000] if isinstance(explanation, str) else "",
    }


def _summarize(
    visible: list[dict[str, Any]],
    highest: float,
    block: bool,
    llm_confirms_same: bool,
) -> str:
    if not visible:
        return "没有找到达到显示下限的相似题目，可以继续提交。"
    percent = round(highest * 100)
    base = f"发现 {len(visible)} 道候选题，最高相似度约 {percent}%。"
    if block and llm_confirms_same:
        return base + "复核判断存在疑似同题，建议先核实再提交。"
    if block:
        return base + "相似度很高，建议先核实是否为原题再提交。"
    return base + "相似度未达到拦截线，请出题人自行确认。"
