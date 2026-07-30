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

import ipaddress
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_UUID_RE = re.compile(
    r"^(?:"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-"
    r"[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
    r"|00000000-0000-0000-0000-000000000000"
    r"|ffffffff-ffff-ffff-ffff-ffffffffffff"
    r")$"
)
_UTC_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_JS_TRIM_CHARACTERS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_PROBLEM_TYPES = {"traditional", "interactive", "submit_answer"}
MAX_CANDIDATES = 50
MAX_RESPONSE_BYTES = 2_000_000
_REQUEST_KEYS = {"apiVersion", "requestId", "contentHash", "problem"}
_PROBLEM_KEYS = {"title", "type", "tagIds", "basicStatement"}


class ContractError(ValueError):
    """请求或即将发出的响应不符合契约。"""


def parse_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("请求正文必须是 JSON 对象。")
    _require_exact_keys(payload, _REQUEST_KEYS, "请求正文")
    if payload.get("apiVersion") != "1":
        raise ContractError("apiVersion 必须是字符串 \"1\"。")

    request_id = payload.get("requestId")
    if not isinstance(request_id, str) or not _is_uuid(request_id):
        raise ContractError("requestId 必须是规范的 UUID。")

    content_hash = payload.get("contentHash")
    if not isinstance(content_hash, str) or not _HASH_RE.match(content_hash):
        raise ContractError("contentHash 必须是 64 位小写十六进制。")

    problem = payload.get("problem")
    if not isinstance(problem, dict):
        raise ContractError("problem 缺失。")
    _require_exact_keys(problem, _PROBLEM_KEYS, "problem")

    title = problem.get("title")
    if not isinstance(title, str):
        raise ContractError("problem.title 不合法。")
    normalized_title = _js_trim(title)
    if not (1 <= _utf16_length(normalized_title) <= 200):
        raise ContractError("problem.title 不合法。")

    problem_type = problem.get("type")
    if not isinstance(problem_type, str) or problem_type not in _PROBLEM_TYPES:
        raise ContractError("problem.type 不合法。")

    tag_ids = problem.get("tagIds")
    if not isinstance(tag_ids, list) or not (1 <= len(tag_ids) <= 30):
        raise ContractError("problem.tagIds 数量不合法。")
    if not all(
        isinstance(tag, str) and 1 <= _utf16_length(tag) <= 120 for tag in tag_ids
    ):
        raise ContractError("problem.tagIds 内容不合法。")

    basic_statement = problem.get("basicStatement")
    if not isinstance(basic_statement, str) or not (
        1 <= _utf16_length(basic_statement) <= 500_000
    ):
        raise ContractError("problem.basicStatement 不合法。")

    return {
        "request_id": request_id,
        "content_hash": content_hash,
        "title": normalized_title,
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
    if not isinstance(content_hash, str) or not _HASH_RE.match(content_hash):
        raise ContractError("响应的 contentHash 不合法。")
    if not isinstance(candidates, list):
        raise ContractError("响应候选必须是数组。")
    if not isinstance(block_submission, bool):
        raise ContractError("响应的拦截建议必须是布尔值。")
    if not isinstance(message, str):
        raise ContractError("响应说明必须是文本。")

    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(normalized) >= MAX_CANDIDATES:
            break
        if not isinstance(candidate, dict):
            raise ContractError("响应候选必须是对象。")
        raw_similarity = candidate.get("similarity")
        if isinstance(raw_similarity, bool) or not isinstance(raw_similarity, (int, float)):
            raise ContractError("candidate.similarity 必须是有限数字。")
        similarity = float(raw_similarity)
        if not math.isfinite(similarity) or not (0.0 <= similarity <= 1.0):
            raise ContractError("candidate.similarity 超出范围。")
        item: dict[str, Any] = {
            "source": _bounded_required(candidate.get("source"), 80, "candidate.source"),
            "externalId": _bounded_required(
                candidate.get("externalId"), 200, "candidate.externalId"
            ),
            "title": _bounded_required(candidate.get("title"), 200, "candidate.title"),
            "similarity": similarity,
        }
        url = _safe_http_url(candidate.get("url"))
        if url is not None:
            item["url"] = url
        same_problem = candidate.get("sameProblemSuggestion")
        if same_problem is not None:
            if not isinstance(same_problem, bool):
                raise ContractError("candidate.sameProblemSuggestion 必须是布尔值。")
            item["sameProblemSuggestion"] = same_problem
        explanation = candidate.get("explanation")
        if explanation is not None:
            item["explanation"] = _bounded_required(
                explanation, 2000, "candidate.explanation"
            )
        normalized.append(item)

    trimmed_message = _bounded_required(message, 2000, "recommendation.message")
    normalized_checked_at = checked_at or _utc_now_z()
    _validate_utc_z(normalized_checked_at)
    result = {
        "apiVersion": "1",
        "contentHash": content_hash,
        "checkedAt": normalized_checked_at,
        "candidates": normalized,
        "recommendation": {
            "blockSubmission": block_submission,
            "message": trimmed_message,
        },
    }
    body = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ContractError("响应内容超过 2MB。")
    return result


def _require_exact_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    if set(value) != expected:
        raise ContractError(f"{path} 字段不完整或包含额外字段。")


def _is_uuid(value: str) -> bool:
    if _UUID_RE.fullmatch(value) is None:
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _bounded_required(value: Any, limit: int, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{path} 必须是文本。")
    trimmed = _js_trim(value)
    if not trimmed:
        raise ContractError(f"{path} 不能为空。")
    return _truncate_utf16(trimmed, limit)


def _safe_http_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    trimmed = _js_trim(value)
    if (
        not trimmed
        or _utf16_length(trimmed) > 2_000
        or "\\" in trimmed
        or any(character.isspace() or ord(character) < 32 for character in trimmed)
    ):
        return None
    try:
        parsed = urlsplit(trimmed)
        hostname = parsed.hostname
        # 访问 port 属性会实际校验端口是否为数字、是否落在合法范围内。
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or not _safe_hostname(hostname)
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return trimmed


def _safe_hostname(hostname: str) -> bool:
    if not hostname or "%" in hostname or "\\" in hostname:
        return False
    if ":" in hostname or all(
        character.isdigit() or character == "." for character in hostname
    ):
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return True
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return False
    if not ascii_hostname or len(ascii_hostname) > 253:
        return False
    labels = ascii_hostname.split(".")
    return all(_HOST_LABEL_RE.fullmatch(label) is not None for label in labels)


def _validate_utc_z(value: Any) -> None:
    if not isinstance(value, str) or _UTC_Z_RE.fullmatch(value) is None:
        raise ContractError("checkedAt 必须是以 Z 结尾的 UTC 时间。")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ContractError("checkedAt 必须是有效时间。") from error
    if parsed.utcoffset() != timedelta(0):
        raise ContractError("checkedAt 必须是 UTC 时间。")


def _utf16_length(value: str) -> int:
    """按 JavaScript 字符串长度计数；非基本平面的字符占两个 UTF-16 单元。"""
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _js_trim(value: str) -> str:
    """按 JavaScript String.trim 使用的空白字符集合去掉两端空白。"""
    return value.strip(_JS_TRIM_CHARACTERS)


def _truncate_utf16(value: str, limit: int) -> str:
    used = 0
    end = 0
    for index, character in enumerate(value):
        width = 2 if ord(character) > 0xFFFF else 1
        if used + width > limit:
            break
        used += width
        end = index + 1
    return value[:end]
