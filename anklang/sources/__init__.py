"""源插件框架：每个"来源"（source）是 anklang/sources/ 下的一个子包，只要实现约定
的两个符号（SOURCE_NAME、fetch_new_problems），就能被框架自动发现并纳入抓取入库
流程，不需要在别处手工注册。

约定（对应 docs/plan.md 4.1 节）：
  SOURCE_NAME: str
      这个来源的标识，写入 problems 表的 source 列，同时也是 ingest_cursor 表按
      来源隔离游标的 key。必须在全部来源里唯一。
  fetch_new_problems(since: str | None) -> list[RawProblem]
      抓取"自 since 之后"的新题或更新题；since 为 None 表示第一次抓取（是全量还是
      来源自己定义的起点，由来源自己决定）。非空 since 一律是带毫秒、以 Z 结尾的
      UTC 时间；来源返回的每道题也必须带同格式的 updated_at。统一格式是为了让框架
      能可靠挑出较新版本，并防止并发旧任务把游标倒退。分页编号等来源内部状态应由
      来源自己保存，不能混用这个公共时间游标。

框架不对来源的抓取方式做任何假设（HTTP 请求、读本地文件、调官方 API 都可以），
只负责：
  1. 用 pkgutil 发现 anklang/sources/ 下所有实现了上述约定的子包；
  2. 调用每个来源的 fetch_new_problems，拿到 RawProblem 列表；
  3. 规范化题面、计算内容哈希、（若 embedding 服务可用）计算向量、按
     (source, external_id) 去重后写入 ProblemStore；
  4. 推进该来源的增量游标。
第 2-4 步的编排逻辑在 anklang/ingest.py，不在这个文件里——这里只放"发现"逻辑和
双方共用的 RawProblem 数据结构。

每个来源自己的中间数据（分页游标、去重指纹、限流状态、账号凭据等）只应该存在自己
的子目录或自己在 ingest_cursor 表里的那一行，不与其他来源共享，这个隔离原则和
Urmotiv 自己的插件规范（"插件如需保存数据，必须声明独立数据库命名空间"）是同一
治理思路，虽然 Anklang 是独立服务，沿用同一套思路方便未来维护者理解。
"""
from __future__ import annotations

import ipaddress
import importlib
import pkgutil
import re
from dataclasses import dataclass, replace
from datetime import datetime
from types import ModuleType
from urllib.parse import urlsplit

from anklang.metadata import MetadataContractError, canonicalize_metadata

_SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_UPDATED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_HOST_LABEL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_TEXT_TRIM_CHARACTERS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_MAX_EXTERNAL_ID_LENGTH = 200
_MAX_TITLE_LENGTH = 200
_MAX_STATEMENT_LENGTH = 500_000
_MAX_URL_LENGTH = 2_048


class SourceContractError(RuntimeError):
    """来源模块的名称或返回数据不符合公开约定。错误信息不得包含题面。"""


@dataclass(frozen=True)
class RawProblem:
    """源插件抓取到的一道原始题目，尚未规范化、尚未计算向量——这两步由框架
    （anklang/ingest.py）统一处理，源插件不需要自己做。"""

    external_id: str
    title: str
    statement: str
    url: str | None = None
    updated_at: str | None = None
    """该题目自己的更新时间。若提供，必须是带毫秒、以 Z 结尾的 UTC 时间，例如
    2026-01-01T00:00:00.000Z。统一格式让框架能可靠判断哪个版本较新并推进游标。
    不提供时仍可首次入库，但同一题号之后出现不同内容时，框架会保守拒绝覆盖，
    因为它无法判断哪份内容更新。"""
    metadata: dict | None = None
    """可选的公开展示元数据，仅允许标量值，容量受 anklang/metadata.py 约束；
    不参加内容哈希、向量或相似度计算。"""
    raw_ref: str | None = None
    """指向原始抓取产物存放位置的引用（例如本地缓存文件路径、原始响应的某个 ID），
    便于排查问题；不代表要长期保留大文件，也不会被写入 ProblemStore。"""


def discover_source_modules() -> list[ModuleType]:
    """发现 anklang/sources/ 下所有实现了源插件约定的子包，按 SOURCE_NAME 排序返回，
    保证多次调用的顺序稳定。"""
    package = importlib.import_module("anklang.sources")
    modules: list[ModuleType] = []
    for module_info in pkgutil.iter_modules(package.__path__):
        if not module_info.ispkg:
            continue
        module = importlib.import_module(f"anklang.sources.{module_info.name}")
        if hasattr(module, "SOURCE_NAME") and hasattr(module, "fetch_new_problems"):
            modules.append(module)
    return validate_source_modules(modules)


def validate_source_modules(modules: list[ModuleType]) -> list[ModuleType]:
    """校验来源名称和入口，防止两个来源共用游标与题目编号空间。"""
    seen: set[str] = set()
    validated: list[ModuleType] = []
    for module in modules:
        name = getattr(module, "SOURCE_NAME", None)
        fetch = getattr(module, "fetch_new_problems", None)
        if not isinstance(name, str) or _SOURCE_NAME_RE.fullmatch(name) is None:
            raise SourceContractError("来源名称必须使用小写字母、数字、下划线或连字符。")
        if name in seen:
            raise SourceContractError("发现重复的来源名称。")
        if not callable(fetch):
            raise SourceContractError("来源缺少可调用的数据读取入口。")
        seen.add(name)
        validated.append(module)
    validated.sort(key=lambda module: module.SOURCE_NAME)
    return validated


def is_valid_source_updated_at(value: object) -> bool:
    """判断来源更新时间是否为可直接排序的规范 UTC 时间。"""
    if not isinstance(value, str) or _UPDATED_AT_RE.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def validate_raw_problem(value: object) -> RawProblem:
    """在调用付费服务或写入数据库前，完整检查一条来源记录。"""
    if not isinstance(value, RawProblem):
        raise SourceContractError("来源列表包含不符合约定的题目记录。")
    if not _valid_required_text(value.external_id, _MAX_EXTERNAL_ID_LENGTH):
        raise SourceContractError("来源题号必须是非空短文本。")
    if not _valid_required_text(value.title, _MAX_TITLE_LENGTH):
        raise SourceContractError("来源题目名称必须是非空短文本。")
    if not _valid_required_text(value.statement, _MAX_STATEMENT_LENGTH):
        raise SourceContractError("来源题面必须是非空文本且不能超过长度上限。")
    if value.url is not None and not _is_safe_http_url(value.url):
        raise SourceContractError("来源题目链接不合法。")
    if value.updated_at is not None and not is_valid_source_updated_at(
        value.updated_at
    ):
        raise SourceContractError("来源更新时间必须是规范的 UTC 时间。")
    try:
        metadata = canonicalize_metadata(value.metadata)
    except MetadataContractError:
        raise SourceContractError("来源元数据不符合约束。") from None
    if metadata != value.metadata:
        return replace(value, metadata=metadata)
    return value


def _valid_required_text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and _has_visible_text(value)
        and _utf16_length(value) <= limit
    )


def _has_visible_text(value: str) -> bool:
    return bool(value.strip(_TEXT_TRIM_CHARACTERS))


def _utf16_length(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _is_safe_http_url(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or _utf16_length(value) > _MAX_URL_LENGTH
        or "\\" in value
        or any(
            character.isspace()
            or ord(character) < 32
            or 127 <= ord(character) <= 159
            for character in value
        )
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # 读取 port 会检查端口是否为数字以及是否在 0 到 65535 之间；
        # 0 不能作为服务的连接端口，因此在下方单独拒绝。
        port = parsed.port
    except ValueError:
        return False
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or port == 0
        or authority.endswith(":")
        or parsed.username is not None
        or parsed.password is not None
        or not _is_safe_hostname(hostname)
    ):
        return False
    return True


def _is_safe_hostname(hostname: str) -> bool:
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
    return all(
        _HOST_LABEL_RE.fullmatch(label) is not None
        for label in ascii_hostname.split(".")
    )
