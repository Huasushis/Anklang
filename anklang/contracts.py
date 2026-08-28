"""Urmotiv <-> Anklang 的接口契约与校验。

字段、取值范围严格对齐 Urmotiv 已实现的客户端（plugins/anklang/src/index.ts 里的
anklangRequestSchema / anklangResultSchema）。任何不一致都会被 Urmotiv 侧的 zod
拒绝，所以这里宁可自己先报错，也不要发出不合规的响应。

关键约束（容易踩坑）：
- 路径和正文版本必须严格一致；v1 恒为 "1"，v2 恒为 "2"；
- 查询响应里 contentHash 必须原样回显请求里的值；入库响应返回规范化题面的摘要；
- checkedAt 和入库 updatedAt 必须是带 Z 的 UTC 时间；
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

from .metadata import MetadataContractError, canonicalize_metadata

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
_COMPLETION_STATUSES = {"complete", "partial", "unavailable"}
_NONCOMPLETE_REASON_CODES = {
    "search_timeout",
    "search_rate_limited",
    "search_backend_unavailable",
    "search_backend_invalid",
    "search_partial",

    "service_unavailable",
    "service_invalid_response",
    "internal_error",
}
MAX_CANDIDATES = 50
MAX_RESPONSE_BYTES = 2_000_000
_REQUEST_KEYS = {"apiVersion", "requestId", "contentHash", "problem"}
_PROBLEM_KEYS = {"title", "type", "tagIds", "basicStatement"}
_UPSERT_REQUEST_KEYS = {"apiVersion", "requestId", "externalId", "updatedAt", "problem"}
_UPSERT_PROBLEM_KEYS = {"title", "basicStatement"}
_UPSERT_RESULT_KEYS = {
    "apiVersion",
    "requestId",
    "source",
    "externalId",
    "contentHash",
    "outcome",
}
_UPSERT_OUTCOMES = {"inserted", "updated", "unchanged"}
_V2_RESULT_KEYS = {
    "apiVersion",
    "contentHash",
    "checkedAt",
    "completion",
    "candidates",
}


class ContractError(ValueError):
    """请求或即将发出的响应不符合契约。"""


def parse_request(payload: Any, expected_api_version: str = "1") -> dict[str, Any]:
    if expected_api_version not in {"1", "2"}:
        raise ValueError("服务端请求版本配置不合法。")
    if not isinstance(payload, dict):
        raise ContractError("请求正文必须是 JSON 对象。")
    _require_exact_keys(payload, _REQUEST_KEYS, "请求正文")
    if payload.get("apiVersion") != expected_api_version:
        raise ContractError(f"apiVersion 必须是字符串 \"{expected_api_version}\"。")

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

def parse_upsert_request(payload: Any) -> dict[str, Any]:
    """解析 Urmotiv 单题增量入库请求；命名空间和业务属性不属于此契约。"""

    if not isinstance(payload, dict):
        raise ContractError("请求正文必须是 JSON 对象。")
    _require_exact_keys(payload, _UPSERT_REQUEST_KEYS, "请求正文")
    if payload.get("apiVersion") != "1":
        raise ContractError('apiVersion 必须是字符串 "1"。')

    request_id = payload.get("requestId")
    if not isinstance(request_id, str) or not _is_uuid(request_id):
        raise ContractError("requestId 必须是规范的 UUID。")

    external_id = _normalize_required_input(
        payload.get("externalId"), 200, "externalId"
    )
    updated_at = _canonical_updated_at(payload.get("updatedAt"), "updatedAt")

    problem = payload.get("problem")
    if not isinstance(problem, dict):
        raise ContractError("problem 缺失。")
    _require_exact_keys(problem, _UPSERT_PROBLEM_KEYS, "problem")

    title = _normalize_required_input(problem.get("title"), 200, "problem.title")
    basic_statement = problem.get("basicStatement")
    if (
        not isinstance(basic_statement, str)
        or not (1 <= _utf16_length(basic_statement) <= 500_000)
        or not _js_trim(basic_statement)
    ):
        raise ContractError("problem.basicStatement 不合法。")

    return {
        "request_id": request_id,
        "external_id": external_id,
        "updated_at": updated_at,
        "title": title,
        "basic_statement": basic_statement,
    }



def build_upsert_result(
    *,
    request_id: str,
    external_id: str,
    content_hash: str,
    outcome: str,
) -> dict[str, Any]:
    """构造严格的单题入库成功响应；来源固定为 ``urmotiv``。"""

    if not isinstance(request_id, str) or not _is_uuid(request_id):
        raise ContractError("响应的 requestId 不合法。")
    external_id = _normalize_required_input(external_id, 200, "响应的 externalId")
    if not isinstance(content_hash, str) or not _HASH_RE.fullmatch(content_hash):
        raise ContractError("响应的 contentHash 不合法。")
    if outcome not in _UPSERT_OUTCOMES:
        raise ContractError("响应的 outcome 不合法。")
    result = {
        "apiVersion": "1",
        "requestId": request_id,
        "source": "urmotiv",
        "externalId": external_id,
        "contentHash": content_hash,
        "outcome": outcome,
    }
    _validate_response_size(result)
    return result


def validate_upsert_result(payload: Any) -> dict[str, Any]:
    """重新验证即将发出的单题入库成功响应。"""

    if not isinstance(payload, dict):
        raise ContractError("入库响应必须是对象。")
    _require_exact_keys(payload, _UPSERT_RESULT_KEYS, "入库响应")
    normalized = build_upsert_result(
        request_id=payload.get("requestId"),
        external_id=payload.get("externalId"),
        content_hash=payload.get("contentHash"),
        outcome=payload.get("outcome"),
    )
    if payload.get("apiVersion") != "1" or payload.get("source") != "urmotiv":
        raise ContractError("入库响应的版本或来源不合法。")
    if normalized != payload:
        raise ContractError("入库响应包含非规范字段或值。")
    return normalized
def _utc_now_z() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def utc_now_z() -> str:
    """生成契约使用的毫秒精度 UTC 时间。"""

    return _utc_now_z()


def build_result(
    content_hash: str,
    candidates: list[dict[str, Any]],
    checked_at: str | None = None,
) -> dict[str, Any]:
    """构造严格的 v1 查询结果；不携带判定、拦截或复核字段。"""

    if not isinstance(content_hash, str) or not _HASH_RE.match(content_hash):
        raise ContractError("响应的 contentHash 不合法。")
    if not isinstance(candidates, list):
        raise ContractError("响应候选必须是数组。")

    normalized = _normalize_candidates(candidates)
    # v1 契约固定不携带元数据；即使后端返回了 metadata 也保持 v1 字段形状不变。
    normalized = [
        {key: value for key, value in candidate.items() if key != "metadata"}
        for candidate in normalized
    ]
    normalized_checked_at = checked_at or _utc_now_z()
    _parse_utc_z(normalized_checked_at, "checkedAt")
    result = {
        "apiVersion": "1",
        "contentHash": content_hash,
        "checkedAt": normalized_checked_at,
        "candidates": normalized,
    }
    _validate_response_size(result)
    return result


def build_v2_result(
    content_hash: str,
    candidates: list[dict[str, Any]],
    completion: dict[str, Any],
    checked_at: str | None = None,
) -> dict[str, Any]:
    """构造并交叉校验 v2 查询结果。"""

    if not isinstance(content_hash, str) or not _HASH_RE.match(content_hash):
        raise ContractError("响应的 contentHash 不合法。")
    if not isinstance(candidates, list):
        raise ContractError("响应候选必须是数组。")

    normalized = _normalize_candidates(candidates)
    normalized_checked_at = checked_at or _utc_now_z()
    _parse_utc_z(normalized_checked_at, "checkedAt")
    normalized_completion = _normalize_completion(completion)

    status = normalized_completion["status"]
    if status == "unavailable" and normalized:
        raise ContractError("不可用结果不能携带候选。")

    result = {
        "apiVersion": "2",
        "contentHash": content_hash,
        "checkedAt": normalized_checked_at,
        "completion": normalized_completion,
        "candidates": normalized,
    }
    _validate_response_size(result)
    return result


def validate_v2_result(payload: Any) -> dict[str, Any]:
    """重新验证即将发出的完整 v2 查询结果。"""

    if not isinstance(payload, dict):
        raise ContractError("v2 响应必须是对象。")
    _require_exact_keys(payload, _V2_RESULT_KEYS, "v2 响应")
    if payload.get("apiVersion") != "2":
        raise ContractError("v2 响应版本不合法。")
    normalized = build_v2_result(
        content_hash=payload.get("contentHash"),
        candidates=payload.get("candidates"),
        completion=payload.get("completion"),
        checked_at=payload.get("checkedAt"),
    )
    if normalized != payload:
        raise ContractError("v2 响应包含非规范字段或值。")
    return normalized


def _normalize_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        raw_metadata = candidate.get("metadata")
        if raw_metadata is not None:
            try:
                metadata = canonicalize_metadata(raw_metadata)
            except MetadataContractError:
                raise ContractError(
                    "candidate.metadata 不符合元数据约束。"
                ) from None
            if metadata is not None:
                item["metadata"] = metadata
        normalized.append(item)
    return normalized


def _normalize_completion(completion: Any) -> dict[str, Any]:
    if not isinstance(completion, dict):
        raise ContractError("completion 必须是对象。")
    allowed_keys = {"status", "reasonCode", "retryable"}
    if "retryAfterSeconds" in completion:
        allowed_keys.add("retryAfterSeconds")
    _require_exact_keys(completion, allowed_keys, "completion")

    status = completion.get("status")
    reason = completion.get("reasonCode")
    retryable = completion.get("retryable")
    if status not in _COMPLETION_STATUSES:
        raise ContractError("completion.status 不合法。")
    if not isinstance(retryable, bool):
        raise ContractError("completion.retryable 必须是布尔值。")
    if status == "complete":
        if reason != "complete" or retryable or "retryAfterSeconds" in completion:
            raise ContractError("完整结果必须使用固定的完成状态。")
    elif reason not in _NONCOMPLETE_REASON_CODES:
        raise ContractError("completion.reasonCode 不合法。")

    normalized: dict[str, Any] = {
        "status": status,
        "reasonCode": reason,
        "retryable": retryable,
    }
    if "retryAfterSeconds" in completion:
        retry_after = completion["retryAfterSeconds"]
        if (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, int)
            or not 1 <= retry_after <= 86_400
            or not retryable
        ):
            raise ContractError("completion.retryAfterSeconds 不合法。")
        normalized["retryAfterSeconds"] = retry_after
    return normalized




def _validate_response_size(result: dict[str, Any]) -> None:
    body = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ContractError("响应内容超过 2MB。")


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


def _normalize_required_input(value: Any, limit: int, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{path} 必须是文本。")
    trimmed = _js_trim(value)
    if not trimmed:
        raise ContractError(f"{path} 不能为空。")
    if _utf16_length(trimmed) > limit:
        raise ContractError(f"{path} 超过长度上限。")
    return trimmed


def _canonical_updated_at(value: Any, path: str) -> str:
    parsed = _parse_utc_z(value, path)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond:06d}Z"


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


def _parse_utc_z(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or _UTC_Z_RE.fullmatch(value) is None:
        raise ContractError(f"{path} 必须是以 Z 结尾的 UTC 时间。")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ContractError(f"{path} 必须是有效时间。") from error
    if parsed.utcoffset() != timedelta(0):
        raise ContractError(f"{path} 必须是 UTC 时间。")
    return parsed


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
