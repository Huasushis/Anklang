"""环境变量配置。启动时读取并校验，缺失关键项直接报错退出。

密钥只保存在内存里；任何日志、健康检查、错误响应都不允许输出它们。

Anklang 是 is-my-problem-new（MIT，Copyright (c) 2023 Ziqian Zhong）的最小直接改编：
保留原题相似度检索主链，支持独立的 yuantiji 公共来源和可选本地索引。只有在选择
local/hybrid 时才需要运行期配置 OpenAI 兼容的 embedding 提供方。
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


def _read_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = os.environ.get(name, "").strip() or default
    if value not in allowed:
        raise ConfigError(f"{name} 必须是 {'/'.join(allowed)} 之一。")
    return value


def _read_external_base_url(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip() or default
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise ConfigError(f"{name} 不是合法地址。") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not _valid_hostname(parsed.hostname)
        or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ConfigError(f"{name} 必须是无路径、账号或参数的 HTTPS 地址；本机测试可用 HTTP。")
    return value.rstrip("/")


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
    # Direct AppConfig construction in embedded/local deployments preserves the local
    # backend. load_config() below deliberately defaults production to yuantiji.
    search_mode: str = "local"
    yuantiji_base_url: str = "https://yuantiji.ac"
    yuantiji_rerank: bool = False
    local_db_path: str = "problems-data/local-index.db"
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

    ingest_enabled = _read_bool("ANKLANG_INGEST_ENABLED")

    return AppConfig(
        port=_read_int("ANKLANG_PORT", 8730, 1, 65535),
        service_token=service_token,
        search_k=_read_int("ANKLANG_SEARCH_K", 8, 1, 20),
        minimum_similarity=_read_float("ANKLANG_MINIMUM_SIMILARITY", 0.5, 0.0, 1.0),
        search_mode=_read_choice(
            "ANKLANG_SEARCH_MODE", "yuantiji", ("yuantiji", "local", "hybrid")
        ),
        yuantiji_base_url=_read_external_base_url(
            "YUANTIJI_BASE_URL", "https://yuantiji.ac"
        ),
        yuantiji_rerank=_read_bool("YUANTIJI_RERANK", False),
        local_db_path=os.environ.get("ANKLANG_LOCAL_DB_PATH", "").strip()
        or "problems-data/local-index.db",
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
