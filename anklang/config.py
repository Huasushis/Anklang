"""环境变量配置。启动时读取并校验，缺失关键项直接报错退出。

密钥只保存在内存里；任何日志、健康检查、错误响应都不允许输出它们。

Anklang 是 is-my-problem-new（MIT，Copyright (c) 2023 Ziqian Zhong）的最小直接改编：
改用阿里云百炼（DashScope）做文本向量，支持源插件实时入库，只暴露查询入/结果出的
查重接口。不包含 yuantiji 反向代理、LLM 复核、标定或结果缓存。
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class ConfigError(RuntimeError):
    pass

_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _read_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} 必须是整数。") from error
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


def _read_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} 必须是数字。") from error
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


def _read_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ConfigError(f"{name} 只能留空，或填写 true/false。")


def _validate_http_url(name: str, raw: str) -> str:
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in raw
    ):
        raise ConfigError(f"{name} 必须是完整的 HTTP/HTTPS 地址。")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        # URL 可能误填了账号、密码等私密内容，异常链也不能回显原值。
        parsed = None
        hostname = None
        port = None
    if parsed is None:
        raise ConfigError(f"{name} 必须是完整的 HTTP/HTTPS 地址。")
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ConfigError(f"{name} 必须是完整的 HTTP/HTTPS 地址。")
    if not _valid_hostname(hostname):
        raise ConfigError(f"{name} 的主机名不合法。")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError(f"{name} 不能包含账号或密码。")
    if parsed.netloc.endswith(":") or (port is not None and not (1 <= port <= 65_535)):
        raise ConfigError(f"{name} 的端口不合法。")
    if "?" in raw or "#" in raw:
        raise ConfigError(f"{name} 不能包含查询参数或页面片段。")
    if "\\" in raw:
        raise ConfigError(f"{name} 必须是完整的 HTTP/HTTPS 地址。")
    return raw.rstrip("/")


def _valid_hostname(hostname: str) -> bool:
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


def _read_url(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip() or default
    return _validate_http_url(name, raw)


def _read_optional_url(name: str) -> str | None:
    """读取一个可选的 URL 型配置项：留空返回 None（表示这个可选功能未配置，调用方
    自行决定降级），非空则必须是合法的 HTTP/HTTPS 地址。"""
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return None
    return _validate_http_url(name, raw)


def _read_bind_host(name: str, default: str) -> str:
    """读取 HTTP 监听地址，不允许把 URL、端口或控制字符混进来。"""

    raw = os.environ.get(name, "").strip() or default
    if (
        len(raw) > 253
        or ":" in raw
        or "/" in raw
        or "\\" in raw
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in raw
        )
        or not _valid_hostname(raw)
    ):
        raise ConfigError(f"{name} 必须是合法的 IPv4 地址或主机名，且不能包含端口。")
    return raw


@dataclass(frozen=True)
class AppConfig:
    port: int
    service_token: str | None
    search_k: int
    minimum_similarity: float
    # 本地检索引擎（唯一后端）
    backend: str = "local_engine"
    local_db_path: str = "problems-data/local-index.db"
    local_vector_top_k: int = 20
    local_keyword_top_k: int = 20
    dashscope_base_url: str | None = None
    dashscope_api_key: str | None = None
    dashscope_embedding_model: str = "text-embedding-v4"
    dashscope_embedding_dim: int = 1024
    # 源插件后台抓取（默认关闭，打开后周期性调用源插件入库）
    ingest_enabled: bool = False
    ingest_interval_seconds: int = 3600
    # 默认只在回环地址监听。容器部署必须显式改为 0.0.0.0，并由宿主继续
    # 只绑定 127.0.0.1，避免直接暴露到外网。
    bind_host: str = "127.0.0.1"
    require_service_token: bool = False
    max_in_flight_checks: int = 16
    client_idle_timeout_seconds: float = 15.0
    shutdown_grace_seconds: float = 30.0


def load_config() -> AppConfig:
    require_service_token = _read_bool("ANKLANG_REQUIRE_SERVICE_TOKEN")
    service_token = os.environ.get("ANKLANG_SERVICE_TOKEN", "").strip() or None
    if service_token is not None and len(service_token) < 16:
        raise ConfigError("ANKLANG_SERVICE_TOKEN 至少需要 16 个字符。")
    if require_service_token and service_token is None:
        raise ConfigError(
            "启用 ANKLANG_REQUIRE_SERVICE_TOKEN 时必须配置至少 16 个字符的服务令牌。"
        )

    # DASHSCOPE_* 不是强制项：没配置时本地引擎只做关键词召回（优雅降级），
    # 不因为缺 embedding 凭据就拒绝启动。
    dashscope_base_url = _read_optional_url("DASHSCOPE_BASE_URL")
    dashscope_api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip() or None

    ingest_enabled = _read_bool("ANKLANG_INGEST_ENABLED")

    return AppConfig(
        port=_read_int("ANKLANG_PORT", 8730, 1, 65535),
        service_token=service_token,
        search_k=_read_int("ANKLANG_SEARCH_K", 8, 1, 20),
        minimum_similarity=_read_float("ANKLANG_MINIMUM_SIMILARITY", 0.5, 0.0, 1.0),
        backend="local_engine",
        local_db_path=os.environ.get("ANKLANG_LOCAL_DB_PATH", "").strip()
        or "problems-data/local-index.db",
        local_vector_top_k=_read_int("ANKLANG_LOCAL_VECTOR_TOP_K", 20, 1, 200),
        local_keyword_top_k=_read_int("ANKLANG_LOCAL_KEYWORD_TOP_K", 20, 1, 200),
        dashscope_base_url=dashscope_base_url,
        dashscope_api_key=dashscope_api_key,
        dashscope_embedding_model=os.environ.get("DASHSCOPE_EMBEDDING_MODEL", "").strip()
        or "text-embedding-v4",
        dashscope_embedding_dim=_read_int("DASHSCOPE_EMBEDDING_DIM", 1024, 1, 4096),
        ingest_enabled=ingest_enabled,
        ingest_interval_seconds=_read_int("ANKLANG_INGEST_INTERVAL_SECONDS", 3600, 60, 86_400),
        bind_host=_read_bind_host("ANKLANG_BIND_HOST", "127.0.0.1"),
        require_service_token=require_service_token,
        max_in_flight_checks=_read_int(
            "ANKLANG_MAX_IN_FLIGHT_CHECKS", 16, 1, 256
        ),
        client_idle_timeout_seconds=_read_float(
            "ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS", 15.0, 1.0, 300.0
        ),
        shutdown_grace_seconds=_read_float(
            "ANKLANG_SHUTDOWN_GRACE_SECONDS", 30.0, 1.0, 300.0
        ),
    )
