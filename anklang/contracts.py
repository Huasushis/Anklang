"""Urmotiv <-> Anklang 的接口契约与校验。

字段、取值范围严格对齐 Urmotiv 已实现的客户端（plugins/anklang/src/index.ts 里的
anklangRequestSchema / anklangResultSchema）。任何不一致都会被 Urmotiv 侧的 zod
拒绝，所以这里宁可自己先报错，也不要发出不合规的响应。

关键约束（容易踩坑）：
- apiVersion 恒为字符串 "1"；
- 响应里 contentHash 必须原样回显请求里的值；
- checkedAt 必须是带 Z 的 UTC 时间；
- similarity 在 [0, 1]；candidates 最多 50 条；
- 响应不能出现契约之外的字段（Urmotiv 用 .strict() 校验）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_PROBLEM_TYPES = {"traditional", "interactive", "submit_answer"}
MAX_CANDIDATES = 50


class ContractError(ValueError):
    """请求或即将发出的响应不符合契约。"""


def parse_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("请求正文必须是 JSON 对象。")
    if payload.get("apiVersion") != "1":
        raise ContractError("apiVersion 必须是字符串 \"1\"。")

    request_id = payload.get("requestId")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ContractError("requestId 缺失。")

    content_hash = payload.get("contentHash")
    if not isinstance(content_hash, str) or not _HASH_RE.match(content_hash):
        raise ContractError("contentHash 必须是 64 位小写十六进制。")

    problem = payload.get("problem")
    if not isinstance(problem, dict):
        raise ContractError("problem 缺失。")

    title = problem.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ContractError("problem.title 不合法。")

    problem_type = problem.get("type")
    if problem_type not in _PROBLEM_TYPES:
        raise ContractError("problem.type 不合法。")

    tag_ids = problem.get("tagIds")
    if not isinstance(tag_ids, list) or not (1 <= len(tag_ids) <= 30):
        raise ContractError("problem.tagIds 数量不合法。")
    if not all(isinstance(tag, str) and 1 <= len(tag) <= 120 for tag in tag_ids):
        raise ContractError("problem.tagIds 内容不合法。")

    basic_statement = problem.get("basicStatement")
    if not isinstance(basic_statement, str) or not (1 <= len(basic_statement) <= 500_000):
        raise ContractError("problem.basicStatement 不合法。")

    return {
        "request_id": request_id,
        "content_hash": content_hash,
        "title": title,
        "type": problem_type,
        "tag_ids": list(tag_ids),
        "basic_statement": basic_statement,
    }


def _utc_now_z() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def build_result(
    content_hash: str,
    candidates: list[dict[str, Any]],
    block_submission: bool,
    message: str,
    checked_at: str | None = None,
) -> dict[str, Any]:
    if not _HASH_RE.match(content_hash):
        raise ContractError("响应的 contentHash 不合法。")

    normalized: list[dict[str, Any]] = []
    for candidate in candidates[:MAX_CANDIDATES]:
        similarity = float(candidate["similarity"])
        if not (0.0 <= similarity <= 1.0):
            raise ContractError("candidate.similarity 超出范围。")
        item: dict[str, Any] = {
            "source": _bounded(str(candidate["source"]), 80),
            "externalId": _bounded(str(candidate["externalId"]), 200),
            "title": _bounded(str(candidate["title"]), 200),
            "similarity": similarity,
        }
        url = candidate.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            item["url"] = url[:2000]
        if isinstance(candidate.get("sameProblemSuggestion"), bool):
            item["sameProblemSuggestion"] = candidate["sameProblemSuggestion"]
        explanation = candidate.get("explanation")
        if isinstance(explanation, str) and explanation.strip():
            item["explanation"] = _bounded(explanation, 2000)
        normalized.append(item)

    trimmed_message = _bounded(message, 2000) or "已完成原题检索。"
    return {
        "apiVersion": "1",
        "contentHash": content_hash,
        "checkedAt": checked_at or _utc_now_z(),
        "candidates": normalized,
        "recommendation": {
            "blockSubmission": bool(block_submission),
            "message": trimmed_message,
        },
    }


def _bounded(value: str, limit: int) -> str:
    trimmed = value.strip()
    return trimmed[:limit]
