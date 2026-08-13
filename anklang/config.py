"""环境变量配置。启动时读取并校验，缺失关键项直接报错退出。

密钥只保存在内存里；任何日志、健康检查、错误响应都不允许输出它们。
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .yuantiji import calculate_search_budget_seconds

class ConfigError(RuntimeError):
    pass

_MAX_REVISION_LENGTH = 200
_REVISION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _read_revision(name: str) -> str | None:
    """读取部署构建时注入的代码修订标识（如 Git 短哈希）。

    修订标识由构建/部署流水线通过环境变量注入，而不是在请求时读取 Git 仓库，
    因此只读环境变量。留空返回 None，表示部署未注入修订标识；非空时只允许
    字母、数字、点、下划线和连字符，长度不超过 200，避免把路径、密钥或
    控制字符伪装成修订标识写入响应头。
    """
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return None
    if len(raw) > _MAX_REVISION_LENGTH or _REVISION_RE.fullmatch(raw) is None:
        raise ConfigError(
            f"{name} 只能包含字母、数字、点、下划线和连字符，且不超过 200 个字符。"
        )
    return raw


_MAX_REQUEST_WAIT_SECONDS = 100.0
_LOCAL_EMBEDDING_TIMEOUT_SECONDS = 30.0
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


def _read_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    raw = os.environ.get(name, "").strip() or default
    if raw not in choices:
        raise ConfigError(f"{name} 必须是 {' 或 '.join(choices)} 之一。")
    return raw


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


def _validate_request_wait_budget(
    *,
    backend: str,
    upstream_timeout_seconds: float,
    upstream_minimum_interval_seconds: float,
    upstream_max_retries: int,
    upstream_retry_base_delay_seconds: float,
    local_embedding_enabled: bool,
    llm_review_enabled: bool,
    llm_review_top_n: int,
    llm_timeout_seconds: float,
) -> None:
    """拒绝一次查重可能等待过久的配置组合。

    主系统允许一次外部检查最多等待 120 秒。这里把配置所允许的本地排队、上游请求、
    本地检索的文字转数字请求、再次尝试前的等待和逐个模型复核都算进去，并限制在
    100 秒内，给数据整理、网络传输和主系统自身处理留出至少 20 秒。

    这个计算限制的是配置里的等待数值。Python 标准库能把剩余时间传给每次网络操作，
    但如果对方故意持续、极慢地传输少量数据，不能保证在某个墙钟时刻强制切断连接。
    """

    wait_seconds = 0.0
    if backend == "reverse_proxy":
        wait_seconds += calculate_search_budget_seconds(
            timeout_seconds=upstream_timeout_seconds,
            minimum_interval_seconds=upstream_minimum_interval_seconds,
            max_retries=upstream_max_retries,
            retry_base_delay_seconds=upstream_retry_base_delay_seconds,
        )
    elif local_embedding_enabled:
        # 本地检索在配置了文字转数字服务时会先做一次网络请求；其客户端当前固定
        # 最多等待 30 秒，这段时间同样属于主系统等待查重结果的时间。
        wait_seconds += _LOCAL_EMBEDDING_TIMEOUT_SECONDS
    if llm_review_enabled:
        # 当前实现逐个复核候选，因此最坏时间是每一项超时相加。
        wait_seconds += llm_review_top_n * llm_timeout_seconds
    if wait_seconds > _MAX_REQUEST_WAIT_SECONDS:
        raise ConfigError(
            "查重等待时间的配置组合过长。请减少上游超时、再次尝试次数、请求间隔，"
            "或模型复核数量与超时；按配置计算的最长等待必须不超过 100 秒，"
            "以便在主系统的 120 秒上限内留出处理和传输时间。"
        )


@dataclass(frozen=True)
class AppConfig:
    port: int
    service_token: str | None
    yuantiji_base_url: str
    yuantiji_timeout_seconds: float
    yuantiji_minimum_interval_seconds: float
    search_k: int
    use_rerank: bool
    minimum_similarity: float
    block_threshold: float
    cache_ttl_seconds: int
    cache_max_entries: int
    llm_review_enabled: bool
    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str
    llm_review_top_n: int
    llm_timeout_seconds: float
    # ---- 阶段 2：本地检索引擎（默认不启用，生产环境仍用 reverse_proxy） ----
    backend: str = "reverse_proxy"
    local_db_path: str = "problems-data/local-index.db"
    local_vector_top_k: int = 20
    local_keyword_top_k: int = 20
    dashscope_base_url: str | None = None
    dashscope_api_key: str | None = None
    dashscope_embedding_model: str = "text-embedding-v4"
    dashscope_embedding_dim: int = 1024
    # ---- 阶段 3：源插件后台抓取（默认不启用） ----
    ingest_enabled: bool = False
    ingest_interval_seconds: int = 3600
    # 未经人工标注数据校准时，不允许只凭相似度数字建议拦截投稿。
    similarity_block_enabled: bool = False
    # 上游调用失败后的有限重试与暂停策略。连续失败达到阈值后会暂时停止请求，
    # 避免第三方服务异常时继续施压。
    yuantiji_max_retries: int = 1
    yuantiji_retry_base_delay_seconds: float = 0.5
    yuantiji_circuit_failure_threshold: int = 3
    yuantiji_circuit_open_seconds: float = 60.0
    yuantiji_health_cache_seconds: float = 60.0
    # 默认只在回环地址监听。容器部署必须显式改为 0.0.0.0，并由宿主继续
    # 只绑定 127.0.0.1，避免直接暴露到外网。
    bind_host: str = "127.0.0.1"
    require_service_token: bool = False
    max_in_flight_checks: int = 16
    client_idle_timeout_seconds: float = 15.0
    shutdown_grace_seconds: float = 30.0
    # 部署构建时注入的代码修订标识；留空表示未注入。由流水线通过环境变量提供，
    # 不在请求时读取 Git 仓库。出现在每个 HTTP 响应头，用于区分部署版本。
    revision: str | None = None


def load_config() -> AppConfig:
    require_service_token = _read_bool("ANKLANG_REQUIRE_SERVICE_TOKEN")
    service_token = os.environ.get("ANKLANG_SERVICE_TOKEN", "").strip() or None
    if service_token is not None and len(service_token) < 16:
        raise ConfigError("ANKLANG_SERVICE_TOKEN 至少需要 16 个字符。")
    if require_service_token and service_token is None:
        raise ConfigError(
            "启用 ANKLANG_REQUIRE_SERVICE_TOKEN 时必须配置至少 16 个字符的服务令牌。"
        )

    llm_review_enabled = _read_bool("ANKLANG_LLM_REVIEW")
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    if llm_review_enabled:
        llm_base_url = _read_url("ANKLANG_LLM_BASE_URL", "")
        llm_api_key = os.environ.get("ANKLANG_LLM_API_KEY", "").strip()
        if llm_api_key == "":
            raise ConfigError("启用 LLM 复核时必须配置 ANKLANG_LLM_API_KEY。")

    # 检索后端选择：生产环境默认仍是 reverse_proxy（转发给 yuantiji.ac）；
    # local_engine 是面向未来自建题库的可选项，见 anklang/backends/local_engine.py。
    backend = _read_choice("ANKLANG_BACKEND", "reverse_proxy", ("reverse_proxy", "local_engine"))

    # DASHSCOPE_* 即使 backend=local_engine 也不是强制项：没配置时本地引擎只做
    # 关键词召回（优雅降级），不因为缺 embedding 凭据就拒绝启动。
    dashscope_base_url = _read_optional_url("DASHSCOPE_BASE_URL")
    dashscope_api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip() or None

    ingest_enabled = _read_bool("ANKLANG_INGEST_ENABLED")
    use_rerank = _read_bool("ANKLANG_USE_RERANK")
    similarity_block_enabled = _read_bool("ANKLANG_SIMILARITY_BLOCK_ENABLED")
    yuantiji_timeout_seconds = _read_float(
        "YUANTIJI_TIMEOUT_SECONDS", 12.0, 1.0, 60.0
    )
    yuantiji_minimum_interval_seconds = _read_float(
        "YUANTIJI_MINIMUM_INTERVAL_SECONDS", 2.0, 0.0, 60.0
    )
    yuantiji_max_retries = _read_int("YUANTIJI_MAX_RETRIES", 1, 0, 3)
    yuantiji_retry_base_delay_seconds = _read_float(
        "YUANTIJI_RETRY_BASE_DELAY_SECONDS", 0.5, 0.0, 5.0
    )
    llm_review_top_n = _read_int("ANKLANG_LLM_REVIEW_TOP_N", 2, 1, 5)
    llm_timeout_seconds = _read_float(
        "ANKLANG_LLM_TIMEOUT_SECONDS", 30.0, 5.0, 120.0
    )
    _validate_request_wait_budget(
        backend=backend,
        upstream_timeout_seconds=yuantiji_timeout_seconds,
        upstream_minimum_interval_seconds=yuantiji_minimum_interval_seconds,
        upstream_max_retries=yuantiji_max_retries,
        upstream_retry_base_delay_seconds=yuantiji_retry_base_delay_seconds,
        local_embedding_enabled=bool(dashscope_base_url and dashscope_api_key),
        llm_review_enabled=llm_review_enabled,
        llm_review_top_n=llm_review_top_n,
        llm_timeout_seconds=llm_timeout_seconds,
    )

    return AppConfig(
        port=_read_int("ANKLANG_PORT", 8730, 1, 65535),
        service_token=service_token,
        yuantiji_base_url=_read_url("YUANTIJI_BASE_URL", "https://yuantiji.ac"),
        # 默认组合把排队、请求间隔、两次各 12 秒的上游等待和再次尝试前等待
        # 合计限制在约 33 秒；调用方超时需按文档设置为至少 120 秒。
        yuantiji_timeout_seconds=yuantiji_timeout_seconds,
        yuantiji_minimum_interval_seconds=yuantiji_minimum_interval_seconds,
        search_k=_read_int("ANKLANG_SEARCH_K", 8, 1, 20),
        use_rerank=use_rerank,
        minimum_similarity=_read_float("ANKLANG_MINIMUM_SIMILARITY", 0.5, 0.0, 1.0),
        block_threshold=_read_float("ANKLANG_BLOCK_THRESHOLD", 0.93, 0.0, 1.0),
        cache_ttl_seconds=_read_int("ANKLANG_CACHE_TTL_SECONDS", 86_400, 60, 604_800),
        cache_max_entries=_read_int("ANKLANG_CACHE_MAX_ENTRIES", 500, 10, 10_000),
        llm_review_enabled=llm_review_enabled,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=os.environ.get("ANKLANG_LLM_MODEL", "deepseek-v4-flash").strip()
        or "deepseek-v4-flash",
        llm_review_top_n=llm_review_top_n,
        llm_timeout_seconds=llm_timeout_seconds,
        backend=backend,
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
        similarity_block_enabled=similarity_block_enabled,
        yuantiji_max_retries=yuantiji_max_retries,
        yuantiji_retry_base_delay_seconds=yuantiji_retry_base_delay_seconds,
        yuantiji_circuit_failure_threshold=_read_int(
            "YUANTIJI_CIRCUIT_FAILURE_THRESHOLD", 3, 1, 20
        ),
        yuantiji_circuit_open_seconds=_read_float(
            "YUANTIJI_CIRCUIT_OPEN_SECONDS", 60.0, 1.0, 900.0
        ),
        yuantiji_health_cache_seconds=_read_float(
            "YUANTIJI_HEALTH_CACHE_SECONDS", 60.0, 1.0, 600.0
        ),
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
        revision=_read_revision("ANKLANG_REVISION"),
    )
