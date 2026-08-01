"""查重结果的判定：整理候选、按阈值决定是否建议拦截，可选调用 LLM 复核。

流程：
1. 过滤掉相似度低于显示下限的候选，按相似度降序；
2. 若开启 LLM 复核且最高相似度达到复核线，用 LLM 判断前 N 组是否同题，
   结果写进对应候选的 sameProblemSuggestion，并用固定说明展示复核结论；
3. 拦截建议：LLM 确认存在同题时建议不要提交；只有管理员明确开启、且阈值已经
   用人工标注数据校准后，才允许只凭最高相似度建议拦截。

LLM 复核失败不影响候选展示，但会把整次结果标为部分完成，禁止缓存和纯相似度拦截。
若同一批复核中另一个本次成功的调用明确确认同题，仍可据此建议拦截。
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
    *,
    search_complete: bool = True,
) -> dict[str, Any]:
    visible: list[dict[str, Any]] = []
    for raw_candidate in raw_candidates:
        if raw_candidate["similarity"] < config.minimum_similarity:
            continue
        candidate = dict(raw_candidate)
        # 这个字段只信任本次进程内成功的 LLM 复核。检索后端或上游即使意外带了
        # 同名字段也不能冒充可信正面结论。
        candidate.pop("sameProblemSuggestion", None)
        visible.append(candidate)
    visible.sort(key=lambda candidate: candidate["similarity"], reverse=True)

    highest = visible[0]["similarity"] if visible else 0.0
    llm_confirms_same = False
    review_failed = False

    if config.llm_review_enabled and visible and highest >= config.minimum_similarity:
        if llm_client is None:
            # 启动配置通常会保证客户端存在；仍把依赖未构造成功视为复核未完成，
            # 不能因为注入或未来重构而悄悄退回纯相似度完整结论。
            review_failed = True
        else:
            for candidate in visible[: config.llm_review_top_n]:
                try:
                    verdict = _llm_review_one(llm_client, config, request, candidate)
                except LlmError:
                    review_failed = True
                    continue
                if verdict is None:
                    review_failed = True
                    continue
                candidate["sameProblemSuggestion"] = verdict["sameProblem"]
                candidate["explanation"] = (
                    "模型复核认为这两道题可能相同，请人工核对来源记录。"
                    if verdict["sameProblem"]
                    else "模型复核没有确认同题，仍请人工核对来源记录。"
                )
                if verdict["sameProblem"]:
                    llm_confirms_same = True

    similarity_blocks = (
        search_complete
        and not review_failed
        and config.similarity_block_enabled
        and highest >= config.block_threshold
    )
    block = similarity_blocks or llm_confirms_same
    message = _summarize(
        visible,
        highest,
        block,
        llm_confirms_same,
        similarity_blocks,
        config.similarity_block_enabled,
        incomplete=(not search_complete or review_failed),
    )
    return {
        "candidates": visible,
        "block_submission": block,
        "message": message,
        "review_failed": review_failed,
        "trusted_same_problem": llm_confirms_same,
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
        "只输出 JSON：{\"sameProblem\": true|false}。"
    )
    user = json.dumps(
        {
            "submitted": {"title": request["title"], "statement": request["basic_statement"][:6000]},
            "candidate": {
                "title": candidate["title"],
                "source": candidate["source"],
                "similarity": candidate["similarity"],
                # 这段文字只在当前请求内交给复核模型，不进入返回结果、缓存或日志。
                "excerpt": (candidate.get("_reviewExcerpt") or "")[:2000],
            },
        },
        ensure_ascii=False,
    )
    data = llm_client.complete_json(
        model=config.llm_model,
        system=system,
        user=user,
        timeout_seconds=config.llm_timeout_seconds,
    )
    same = data.get("sameProblem")
    if not isinstance(same, bool):
        return None
    return {"sameProblem": same}


def _summarize(
    visible: list[dict[str, Any]],
    highest: float,
    block: bool,
    llm_confirms_same: bool,
    similarity_blocks: bool,
    similarity_block_enabled: bool,
    incomplete: bool,
) -> str:
    if incomplete and not visible:
        return "本次检索只完成了一部分，不能据此判断没有相似题，请稍后重试并人工核对。"
    if not visible:
        return "没有找到达到显示下限的相似题目，可以继续提交。"
    percent = round(highest * 100)
    base = f"发现 {len(visible)} 道候选题，最高相似度约 {percent}%。"
    if block and llm_confirms_same:
        return base + "复核判断存在疑似同题，建议先核实再提交。"
    if incomplete:
        return base + "本次检索或复核只完成了一部分，不能据此自动放行或按相似度拦截，请人工核对。"
    if block and similarity_blocks:
        return base + "相似度很高，建议先核实是否为原题再提交。"
    if not similarity_block_enabled:
        return base + "系统未启用只凭相似度自动拦截，请出题人自行确认。"
    return base + "相似度未达到已校准的拦截线，请出题人自行确认。"
