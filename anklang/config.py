"""环境变量配置。启动时读取并校验，缺失关键项直接报错退出。

密钥只保存在内存里；任何日志、健康检查、错误响应都不允许输出它们。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


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


def _read_url(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip() or default
    if not (raw.startswith("http://") or raw.startswith("https://")):
        raise ConfigError(f"{name} 必须是 HTTP/HTTPS 地址。")
    if "@" in raw.split("//", 1)[1].split("/", 1)[0]:
        raise ConfigError(f"{name} 不能包含账号密码。")
    return raw.rstrip("/")


def _read_optional_url(name: str) -> str | None:
    """读取一个可选的 URL 型配置项：留空返回 None（表示这个可选功能未配置，调用方
    自行决定降级），非空则必须是合法的 HTTP/HTTPS 地址。"""
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return None
    if not (raw.startswith("http://") or raw.startswith("https://")):
        raise ConfigError(f"{name} 必须是 HTTP/HTTPS 地址。")
    return raw.rstrip("/")


def _read_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    raw = os.environ.get(name, "").strip() or default
    if raw not in choices:
        raise ConfigError(f"{name} 必须是 {' 或 '.join(choices)} 之一。")
    return raw


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


def load_config() -> AppConfig:
    service_token = os.environ.get("ANKLANG_SERVICE_TOKEN", "").strip() or None
    if service_token is not None and len(service_token) < 16:
        raise ConfigError("ANKLANG_SERVICE_TOKEN 至少需要 16 个字符。")

    llm_review_enabled = os.environ.get("ANKLANG_LLM_REVIEW", "").strip() == "true"
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

    ingest_enabled = os.environ.get("ANKLANG_INGEST_ENABLED", "").strip() == "true"

    return AppConfig(
        port=_read_int("ANKLANG_PORT", 8730, 1, 65535),
        service_token=service_token,
        yuantiji_base_url=_read_url("YUANTIJI_BASE_URL", "https://yuantiji.ac"),
        yuantiji_timeout_seconds=_read_float("YUANTIJI_TIMEOUT_SECONDS", 45.0, 5.0, 120.0),
        yuantiji_minimum_interval_seconds=_read_float(
            "YUANTIJI_MINIMUM_INTERVAL_SECONDS", 2.0, 0.0, 60.0
        ),
        search_k=_read_int("ANKLANG_SEARCH_K", 8, 1, 20),
        use_rerank=os.environ.get("ANKLANG_USE_RERANK", "").strip() == "true",
        minimum_similarity=_read_float("ANKLANG_MINIMUM_SIMILARITY", 0.5, 0.0, 1.0),
        block_threshold=_read_float("ANKLANG_BLOCK_THRESHOLD", 0.93, 0.0, 1.0),
        cache_ttl_seconds=_read_int("ANKLANG_CACHE_TTL_SECONDS", 86_400, 60, 604_800),
        cache_max_entries=_read_int("ANKLANG_CACHE_MAX_ENTRIES", 500, 10, 10_000),
        llm_review_enabled=llm_review_enabled,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=os.environ.get("ANKLANG_LLM_MODEL", "deepseek-v4-flash").strip()
        or "deepseek-v4-flash",
        llm_review_top_n=_read_int("ANKLANG_LLM_REVIEW_TOP_N", 2, 1, 5),
        llm_timeout_seconds=_read_float("ANKLANG_LLM_TIMEOUT_SECONDS", 30.0, 5.0, 120.0),
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
    )
