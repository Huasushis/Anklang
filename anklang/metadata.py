"""有界、标量式的候选元数据规范。

``candidate.metadata`` 是 v2 查重候选的可选扩展，用来携带来源插件提供的
简短、公开、非检索性的题面外信息（例如题目出处或原文链接的展示名）。它
不是通用搜索平台扩展：值只允许标量，容量有硬上限，不允许存放题面或题解，
也不参与 contentHash、向量或相似度。

规范（与 Urmotiv 插件、Fermata 镜像的 zod 校验保持一致，两边按同一套
边界独立实现）：
- 键必须是 ASCII 小写字母、数字、下划线，且以字母开头（^[a-z][a-z0-9_]{0,63}$）；
- 最多 16 个键；
- 值只允许字符串、有限数字、布尔或 null，不允许嵌套对象/数组；
- 字符串值必须是去掉两端空白后的规范文本、不允许为空，并按 UTF-8 字节数
  上限 512；显式缺失用 null 而非空串表达；
- 整包按规范 JSON（ASCII 升序键、紧凑分隔符、ensure_ascii=False）编码后
  不超过 2048 个 UTF-8 字节；
- 空对象或缺失视为“没有元数据”，输出时省略该字段。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_METADATA_KEYS = 16
MAX_METADATA_VALUE_BYTES = 512
MAX_METADATA_BYTES = 2048
# 与 Urmotiv zod 镜像对齐：字符串必须是“已经去掉两端空白”的规范文本。
# 采用与 JavaScript String.trim() 相同的空白字符集合，保证两边一致。
_METADATA_TRIM_CHARACTERS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


class MetadataContractError(ValueError):
    """键、值或整包容量不符合元数据规范。错误信息不得包含题面。"""


def _utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def canonicalize_metadata(value: Any) -> dict[str, Any] | None:
    """把元数据规范化为 ASCII 升序键的规范字典；缺失或空对象返回 None。

    校验失败抛 MetadataContractError（调用方负责转成自己的契约错误）。
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MetadataContractError("元数据必须是 JSON 对象。")
    if len(value) > MAX_METADATA_KEYS:
        raise MetadataContractError(
            f"元数据最多允许 {MAX_METADATA_KEYS} 个键。"
        )

    canonical: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or METADATA_KEY_RE.fullmatch(key) is None:
            raise MetadataContractError("元数据键必须使用小写字母、数字或下划线。")
        if isinstance(item, bool):
            canonical[key] = item
        elif isinstance(item, str):
            # 字符串必须是去掉两端空白后的规范文本：空白串与空串一律拒绝，
            # 显式缺失用 null 而不是空串表达。
            if item != item.strip(_METADATA_TRIM_CHARACTERS):
                raise MetadataContractError("元数据字符串值前后不能有空白。")
            if not item:
                raise MetadataContractError("元数据字符串值不能为空。")
            if _utf8_bytes(item) > MAX_METADATA_VALUE_BYTES:
                raise MetadataContractError("元数据字符串值不能超过 512 字节。")
            canonical[key] = item
        elif isinstance(item, (int, float)):
            if not math.isfinite(item):
                raise MetadataContractError("元数据数字值必须有限。")
            canonical[key] = item
        elif item is None:
            canonical[key] = None
        else:
            raise MetadataContractError("元数据值只允许字符串、数字、布尔或 null。")

    if not canonical:
        return None
    encoded = to_canonical_json(canonical).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise MetadataContractError("元数据整包不能超过 2048 字节。")
    return canonical


def to_canonical_json(metadata: dict[str, Any]) -> str:
    """ASCII 升序键、紧凑分隔符、保留非 ASCII 字符的规范 JSON 序列化。"""

    return json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def parse_canonical_metadata(text: str | None) -> dict[str, Any] | None:
    """从 SQLite 的规范 JSON 文本读回元数据。

    SQL NULL 表示确实没有元数据，返回 None。空文本、空对象、非法 JSON、
    非规范字节或不符合契约的值都属于损坏的存储状态，一律抛
    MetadataContractError 让调用方失败关闭，绝不静默丢弃。错误信息不含值。
    """

    if text is None:
        return None
    if not isinstance(text, str) or not text:
        raise MetadataContractError("存储的元数据为空文本。")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise MetadataContractError("存储的元数据不是合法 JSON。") from None
    canonical = canonicalize_metadata(value)
    if canonical is None:
        raise MetadataContractError("存储的元数据是空对象。")
    if to_canonical_json(canonical) != text:
        raise MetadataContractError("存储的元数据不是规范 JSON。")
    return canonical
