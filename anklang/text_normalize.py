"""题面规范化的最小实现。

docs/plan.md 3.6 节规划了完整的 HTML/PDF/OCR 规范化管线（按 HTML 结构化提取 > PDF
文本层解析 > 图片 OCR > 附件文档解析的优先级降级），但那一整套只有接入真实的
HTML/PDF 来源时才用得上。本次只搭建源插件框架的主体、没有实现任何真实爬虫，所以
这里只做"纯文本输入"的规范化：折叠连续空白、去掉首尾空白。真正接入 HTML/PDF 来源
时，在这里按 plan.md 3.6 节的优先级扩展成多个规范化函数即可，不需要改动调用方
（anklang/ingest.py）的接口。
"""
from __future__ import annotations

import hashlib
import re

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_statement(raw_statement: str) -> str:
    """把原始题面文本规范化：连续空白（含换行、制表符）折叠成一个空格，去掉首尾空白。"""
    return _WHITESPACE_RE.sub(" ", raw_statement).strip()


def content_hash_of(normalized_statement: str) -> str:
    """规范化后题面文本的 sha256 十六进制摘要，用于去重和"内容是否变化"的判断。"""
    return hashlib.sha256(normalized_statement.encode("utf-8")).hexdigest()
