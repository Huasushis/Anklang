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

from .backends import BackendError, BackendSearchResult, SearchBackend
from .backends.local_engine import LocalEngineBackend
from .backends.reverse_proxy import ReverseProxyBackend
from .cache import ResultCache
from .config import AppConfig
from .contracts import ContractError, build_result, parse_request
from .embedding import EmbeddingClient
from .ingest import ingest_once
from .llm import LlmClient
from .review import evaluate
from .store import IndexMetadataError, ProblemStore
from .yuantiji import YuantijiClient

# 合法题面最多 500,000 个 UTF-16 单元；控制字符经 JSON 转义后一个单元可能
# 占 6 字节，因此请求上限要高于 3MB。2MB 只用于响应上限。
_MAX_REQUEST_BYTES = 4_000_000


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
        content_hash = request["content_hash"]
        cache_key = self._current_cache_key(content_hash)
        if cache_key is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            search_result = self.backend.search(
                request["basic_statement"], self.config.search_k
            )
        except BackendError:
            # 第三方服务或本地索引暂时不可用，不应该变成投稿接口的 5xx。返回一个
            # 字段完整、明确不自动拦截的结果，让 Urmotiv 能给出可理解的人工核对提示。
            # 降级结果不写缓存，下一次请求可以立即重新尝试。
            return self._build_unavailable_result(content_hash)
        if search_result.degraded:
            # 不完整检索不能进入“没有命中即可继续提交”的正常判定，也不能触发
            # 可选模型复核或缓存。HTTP 契约只有布尔拦截字段，因此固定写 false，
            # 但 message 必须明确要求人工核对，不能把它解释成自动放行。
            return self._build_unavailable_result(content_hash)
        try:
            decision = evaluate(
                self.config,
                request,
                search_result.candidates,
                self.llm_client,
            )
            result = build_result(
                content_hash=content_hash,
                candidates=decision["candidates"],
                block_submission=decision["block_submission"],
                message=decision["message"],
            )
        except (ContractError, KeyError, TypeError, ValueError, OverflowError):
            # 上游候选结构异常也属于“本次检索未完成”，不能让 Urmotiv 收到一个
            # 无法解释的 5xx，更不能把异常候选内容带进错误信息。
            return self._build_unavailable_result(content_hash)
        if not decision["review_failed"]:
            cache_key = self._result_cache_key(content_hash, search_result)
            if cache_key is not None:
                self.cache.set(cache_key, result)
        return result

    def _current_cache_key(self, content_hash: str) -> str | None:
        if not isinstance(self.backend, LocalEngineBackend):
            # 远程后端继续沿用原有按题面摘要缓存的语义。
            return content_hash
        identity = self.backend.current_cache_identity()
        if identity is None:
            return None
        return _local_cache_key(content_hash, identity)

    def _result_cache_key(
        self,
        content_hash: str,
        search_result: BackendSearchResult,
    ) -> str | None:
        if not isinstance(self.backend, LocalEngineBackend):
            return content_hash
        identity = search_result.cache_identity
        if (
            identity is None
            or self.backend.current_cache_identity() != identity
        ):
            # 检索后索引已变化，旧快照的判断不能登记到新索引身份下。
            return None
        return _local_cache_key(content_hash, identity)

    @staticmethod
    def _build_unavailable_result(content_hash: str) -> dict[str, Any]:
        return build_result(
            content_hash=content_hash,
            candidates=[],
            block_submission=False,
            message="本次未能完成原题检索，请稍后重试并由审题人手工核对。",
        )


def _local_cache_key(content_hash: str, identity: str) -> str:
    return f"local-index:{identity}:{content_hash}"


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
            if (
                info.get("upstreamReady") is False
                or info.get("localStoreReady") is False
                or info.get("indexMetadataReady") is False
                or (
                    info.get("embeddingAvailable") is True
                    and info.get("vectorIndexReady") is False
                )
            ):
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
            return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

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
            except (ValueError, RecursionError):
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
        backend = LocalEngineBackend(
            store=store,
            embedder=embedder,
            vector_top_k=config.local_vector_top_k,
            keyword_top_k=config.local_keyword_top_k,
        )
        try:
            # 新库或没有旧向量的关键词库可安全登记；旧向量身份未知或规格
            # 冲突时保留原库并由后端降级，不让启动过程破坏数据。
            if backend.index_spec is not None:
                store.prepare_embedding_writes(backend.index_spec)
            else:
                store.prepare_keyword_writes()
        except IndexMetadataError:
            pass
        return backend
    yuantiji = YuantijiClient(
        base_url=config.yuantiji_base_url,
        timeout_seconds=config.yuantiji_timeout_seconds,
        minimum_interval_seconds=config.yuantiji_minimum_interval_seconds,
        max_retries=config.yuantiji_max_retries,
        retry_base_delay_seconds=config.yuantiji_retry_base_delay_seconds,
        circuit_failure_threshold=config.yuantiji_circuit_failure_threshold,
        circuit_open_seconds=config.yuantiji_circuit_open_seconds,
        health_cache_seconds=config.yuantiji_health_cache_seconds,
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
