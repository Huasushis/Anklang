"""Anklang HTTP 服务：暴露 Urmotiv 插件调用的查重接口与健康检查。

- GET  /api/v1/health              健康检查（无需令牌），透传当前检索后端的公开信息；
- POST /api/v1/checks/similarity   查重（配置了 ANKLANG_SERVICE_TOKEN 时需要 Bearer 令牌）。

鉴权用常量时间比较；错误响应只给出稳定的中文说明，不泄露上游细节或密钥。

"检索后端"（SearchBackend，见 anklang/backends/__init__.py）是可切换的：默认
reverse_proxy（转发给 yuantiji.ac，阶段 1，生产默认），可选 local_engine（阶段 2
的本地题库向量+关键词混合检索）。AnklangService 本身不关心具体是哪一种，只调用
统一接口，两种后端切换不影响缓存、契约校验、LLM 复核这些通用逻辑。
"""
from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .backends import BackendError, SearchBackend
from .backends.local_engine import LocalEngineBackend
from .backends.reverse_proxy import ReverseProxyBackend
from .cache import ResultCache
from .config import AppConfig
from .contracts import ContractError, build_result, parse_request
from .embedding import EmbeddingClient
from .ingest import ingest_once
from .llm import LlmClient
from .review import evaluate
from .store import ProblemStore
from .yuantiji import YuantijiClient

_MAX_REQUEST_BYTES = 2_000_000


class AnklangService:
    def __init__(
        self,
        config: AppConfig,
        backend: SearchBackend,
        cache: ResultCache,
        llm_client: LlmClient | None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.cache = cache
        self.llm_client = llm_client

    def check_similarity(self, request: dict[str, Any]) -> dict[str, Any]:
        cached = self.cache.get(request["content_hash"])
        if cached is not None:
            return cached

        candidates = self.backend.search(request["basic_statement"], self.config.search_k)
        decision = evaluate(self.config, request, candidates, self.llm_client)
        result = build_result(
            content_hash=request["content_hash"],
            candidates=decision["candidates"],
            block_submission=decision["block_submission"],
            message=decision["message"],
        )
        self.cache.set(request["content_hash"], result)
        return result


def make_handler(service: AnklangService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Anklang/0.1"

        def log_message(self, *_args: Any) -> None:
            # 默认会把整条请求行打印到 stderr，可能含题面片段；这里禁用。
            return

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定的方法名)
            if self.path == "/api/v1/health":
                self._handle_health()
            else:
                self._send(404, {"error": {"code": "NOT_FOUND", "message": "未找到资源。"}})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/v1/checks/similarity":
                self._send(404, {"error": {"code": "NOT_FOUND", "message": "未找到资源。"}})
                return
            if not self._authorized():
                self._send(401, {"error": {"code": "UNAUTHENTICATED", "message": "缺少或无效的令牌。"}})
                return
            payload = self._read_json()
            if payload is None:
                return
            try:
                request = parse_request(payload)
            except ContractError as error:
                self._send(400, {"error": {"code": "INVALID_REQUEST", "message": str(error)}})
                return
            try:
                result = service.check_similarity(request)
            except BackendError:
                self._send(
                    502,
                    {"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "原题检索后端暂时不可用。"}},
                )
                return
            except ContractError as error:
                self._send(500, {"error": {"code": "INVALID_RESULT", "message": str(error)}})
                return
            self._send(200, result)

        def _handle_health(self) -> None:
            info: dict[str, Any] = {
                "status": "ok",
                "service": "anklang",
                "apiVersion": "1",
                "backend": service.config.backend,
            }
            # describe_health() 约定不抛异常，各后端把自己的失败情况体现成状态字段
            # （例如 upstreamReady=False），这里统一根据这些字段判断是否整体降级。
            info.update(service.backend.describe_health())
            if info.get("upstreamReady") is False or info.get("localStoreReady") is False:
                info["status"] = "degraded"
            self._send(200, info)

        def _authorized(self) -> bool:
            expected = service.config.service_token
            if expected is None:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            provided = header[len("Bearer ") :].strip()
            return hmac.compare_digest(provided, expected)

        def _read_json(self) -> Any | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, {"error": {"code": "INVALID_REQUEST", "message": "长度头无效。"}})
                return None
            if length <= 0 or length > _MAX_REQUEST_BYTES:
                self._send(400, {"error": {"code": "INVALID_REQUEST", "message": "请求体大小不合法。"}})
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"error": {"code": "INVALID_REQUEST", "message": "请求体不是有效 JSON。"}})
                return None

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def build_backend(config: AppConfig) -> SearchBackend:
    """按 ANKLANG_BACKEND 配置构造检索后端。生产环境默认走 reverse_proxy 分支，
    与阶段 1 完全一致；local_engine 分支是阶段 2 新增的可选路径。"""
    if config.backend == "local_engine":
        store = ProblemStore(config.local_db_path)
        embedder: EmbeddingClient | None = None
        if config.dashscope_api_key and config.dashscope_base_url:
            embedder = EmbeddingClient(
                base_url=config.dashscope_base_url,
                api_key=config.dashscope_api_key,
                model=config.dashscope_embedding_model,
                dimensions=config.dashscope_embedding_dim,
            )
        return LocalEngineBackend(
            store=store,
            embedder=embedder,
            vector_top_k=config.local_vector_top_k,
            keyword_top_k=config.local_keyword_top_k,
        )
    yuantiji = YuantijiClient(
        base_url=config.yuantiji_base_url,
        timeout_seconds=config.yuantiji_timeout_seconds,
        minimum_interval_seconds=config.yuantiji_minimum_interval_seconds,
    )
    return ReverseProxyBackend(yuantiji, use_rerank=config.use_rerank)


def build_service(config: AppConfig) -> AnklangService:
    backend = build_backend(config)
    cache = ResultCache(ttl_seconds=config.cache_ttl_seconds, max_entries=config.cache_max_entries)
    llm_client: LlmClient | None = None
    if config.llm_review_enabled and config.llm_base_url and config.llm_api_key:
        llm_client = LlmClient(base_url=config.llm_base_url, api_key=config.llm_api_key)
    return AnklangService(config, backend, cache, llm_client)


def _start_background_ingest(
    backend: LocalEngineBackend, config: AppConfig, stop_event: threading.Event
) -> threading.Thread:
    """按 ANKLANG_INGEST_INTERVAL_SECONDS 周期性调用 ingest_once()，实现"源插件
    实时监控"式的增量抓取入库；单轮失败不影响服务本身，等下一轮重试。只在选中
    local_engine 后端且显式打开 ANKLANG_INGEST_ENABLED 时才会启动。
    """

    def _loop() -> None:
        while not stop_event.is_set():
            try:
                ingest_once(backend.store, backend.embedder)
            except Exception:
                pass  # 后台线程不应该因为单轮 ingest 失败而退出
            stop_event.wait(config.ingest_interval_seconds)

    thread = threading.Thread(target=_loop, daemon=True, name="anklang-ingest")
    thread.start()
    return thread


def serve(config: AppConfig) -> None:
    service = build_service(config)
    handler = make_handler(service)
    httpd = ThreadingHTTPServer(("0.0.0.0", config.port), handler)
    stop_event = threading.Event()
    ingest_thread: threading.Thread | None = None
    if config.ingest_enabled and isinstance(service.backend, LocalEngineBackend):
        ingest_thread = _start_background_ingest(service.backend, config, stop_event)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        httpd.server_close()
        if ingest_thread is not None:
            ingest_thread.join(timeout=5.0)
