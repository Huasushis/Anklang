"""Anklang HTTP 服务：暴露 Urmotiv 插件调用的查重接口与健康检查。

- GET  /api/v1/live                存活检查（无需令牌），固定返回，不做任何外部调用；
- GET  /api/v1/ready               就绪检查（无需令牌），只验证本地状态，不做后端/网络调用；
- GET  /api/v1/health              健康检查（无需令牌），透传当前检索后端的公开信息；
- POST /api/v1/checks/similarity   旧版查重；只有完整结果返回 200；
- POST /api/v2/checks/similarity   显式返回完整性和复用策略。

鉴权用常量时间比较；错误响应只给出稳定的中文说明，不泄露上游细节或密钥。

"检索后端"（SearchBackend，见 anklang/backends/__init__.py）是可切换的：默认
reverse_proxy（转发给 yuantiji.ac，阶段 1，生产默认），可选 local_engine（阶段 2
的本地题库向量+关键词混合检索）。AnklangService 本身不关心具体是哪一种，只调用
统一接口，两种后端切换不影响缓存、契约校验、LLM 复核这些通用逻辑。
"""
from __future__ import annotations

import hmac
import hashlib
import json
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .backends import BackendError, BackendSearchResult, CompletionReason, SearchBackend
from .backends.local_engine import LocalEngineBackend
from .backends.reverse_proxy import ReverseProxyBackend
from .cache import ResultCache
from .config import AppConfig
from .contracts import (
    ContractError,
    build_result,
    build_v2_result,
    parse_request,
    utc_now_z,
    validate_v2_result,
)
from .embedding import EmbeddingClient
from .ingest import ingest_once
from .llm import LlmClient
from .review import evaluate
from .store import IndexMetadataError, ProblemStore
from .yuantiji import YuantijiClient

# 合法题面最多 500,000 个 UTF-16 单元；控制字符经 JSON 转义后一个单元可能
# 占 6 字节，因此请求上限要高于 3MB。2MB 只用于响应上限。
_MAX_REQUEST_BYTES = 4_000_000
_V1_SIMILARITY_PATH = "/api/v1/checks/similarity"
_V2_SIMILARITY_PATH = "/api/v2/checks/similarity"
_LIVE_PATH = "/api/v1/live"
_READY_PATH = "/api/v1/ready"
_CACHE_SCHEMA_REVISION = "similarity-outcome-v2"
_BUSY_RETRY_AFTER_SECONDS = 1


class ServiceRuntime:
    """限制在途查重，并为停止接收与有界等待提供一个原子状态。"""

    def __init__(self, max_in_flight_checks: int) -> None:
        if (
            isinstance(max_in_flight_checks, bool)
            or not isinstance(max_in_flight_checks, int)
            or max_in_flight_checks < 1
        ):
            raise ValueError("最大在途查重数必须为正数。")
        self._max_in_flight_checks = max_in_flight_checks
        self._condition = threading.Condition()
        self._accepting = True
        self._in_flight = 0

    def try_begin_check(self) -> bool:
        with self._condition:
            if (
                not self._accepting
                or self._in_flight >= self._max_in_flight_checks
            ):
                return False
            self._in_flight += 1
            return True

    def finish_check(self) -> None:
        with self._condition:
            if self._in_flight <= 0:
                raise RuntimeError("在途查重计数不一致。")
            self._in_flight -= 1
            if self._in_flight == 0:
                self._condition.notify_all()

    def begin_shutdown(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while self._in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    @property
    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight


class AnklangHTTPServer(ThreadingHTTPServer):
    """请求线程不会把退出拖成无界等待，也不打印原始异常。"""

    daemon_threads = True
    block_on_close = False

    def handle_error(self, _request: Any, _client_address: Any) -> None:
        # 标准库默认会把异常堆栈写到 stderr。异常可能来自处理题面的路径，
        # 因此生产服务器只返回固定响应或静默关闭已经断开的连接。
        return


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
        self._pipeline_identity = _pipeline_identity(config)

    def check_similarity(
        self,
        request: dict[str, Any],
        *,
        api_version: str = "2",
    ) -> dict[str, Any]:
        """返回一份经过严格校验的 v2 结果，供两个 HTTP 版本安全投影。

        ``api_version`` 只参与缓存命名空间，防止未来两个入口处理步骤分化后跨版本
        复用；结果本身始终是信息更完整的 v2 内部形状。
        """

        if api_version not in {"1", "2"}:
            raise ValueError("服务端 API 版本不合法。")
        content_hash = request["content_hash"]
        cache_lookup = self._current_cache_lookup(request, api_version)
        if cache_lookup is not None:
            cache_key, initial_backend_identity = cache_lookup
            cached = self.cache.get(cache_key)
            if cached is not None:
                if not isinstance(self.backend, LocalEngineBackend):
                    return cached
                # cache.get() 与返回结果之间，后台 ingest 或另一个 SQLite 连接
                # 仍可能改变索引。再次做 O(1) 身份门禁；身份包含 data_version
                # 和当前进程写入代际，所以内容被改回原值的 ABA 窗口也会失配。
                confirmed_identity = self.backend.current_cache_identity()
                if (
                    confirmed_identity is not None
                    and confirmed_identity == initial_backend_identity
                ):
                    return cached

        try:
            search_result = self.backend.search(
                request["basic_statement"], self.config.search_k
            )
        except BackendError as error:
            return self._build_unavailable_result(
                content_hash,
                reason_code=error.reason_code,
                retryable=error.retryable,
                retry_after_seconds=error.retry_after_seconds,
            )
        except Exception:
            # 不记录异常文本，避免第三方正文、路径或配置值经异常链进入日志。
            return self._build_unavailable_result(
                content_hash,
                reason_code="internal_error",
                retryable=True,
            )

        if search_result.status == "unavailable":
            return self._build_unavailable_result(
                content_hash,
                reason_code=search_result.reason_code,
                retryable=search_result.retryable,
                retry_after_seconds=search_result.retry_after_seconds,
            )
        try:
            decision = evaluate(
                self.config,
                request,
                search_result.candidates,
                self.llm_client,
                search_complete=search_result.status == "complete",
            )
        except (ContractError, KeyError, TypeError, ValueError, OverflowError):
            return self._build_unavailable_result(
                content_hash,
                reason_code="service_invalid_response",
                retryable=False,
            )

        try:
            review_failed = decision["review_failed"]
            trusted_same_problem = decision["trusted_same_problem"]
            if not isinstance(review_failed, bool) or not isinstance(
                trusted_same_problem, bool
            ):
                raise TypeError("内部复核状态必须是布尔值。")
        except (KeyError, TypeError):
            return self._build_unavailable_result(
                content_hash,
                reason_code="service_invalid_response",
                retryable=False,
            )

        status = search_result.status
        reason_code = search_result.reason_code
        retryable = search_result.retryable
        retry_after_seconds = search_result.retry_after_seconds
        if review_failed:
            status = "partial"
            if search_result.status == "complete":
                reason_code = "review_unavailable"
                retryable = True
                retry_after_seconds = None

        if (
            status != "complete"
            and decision.get("block_submission") is True
            and not trusted_same_problem
        ):
            return self._build_unavailable_result(
                content_hash,
                reason_code="service_invalid_response",
                retryable=False,
            )

        completion = _completion(
            status=status,
            reason_code=reason_code,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )
        checked_at = utc_now_z()

        if status == "complete":
            result_cache_key = self._result_cache_key(
                request,
                api_version,
                search_result,
            )
            if result_cache_key is not None:
                try:
                    expires_at = self.cache.expires_at_for(checked_at)
                    result = build_v2_result(
                        content_hash=content_hash,
                        candidates=decision["candidates"],
                        block_submission=decision["block_submission"],
                        message=decision["message"],
                        completion=completion,
                        reuse={"policy": "allowed", "expiresAt": expires_at},
                        checked_at=checked_at,
                    )
                    self.cache.set(
                        result_cache_key,
                        result,
                        checked_at=checked_at,
                    )
                    return result
                except (ContractError, ValueError, TypeError, OverflowError):
                    # 缓存故障不改变已经完成的检索，只把本次结果标为不可复用。
                    pass

        try:
            return build_v2_result(
                content_hash=content_hash,
                candidates=decision["candidates"],
                block_submission=decision["block_submission"],
                message=decision["message"],
                completion=completion,
                reuse={"policy": "no-store"},
                checked_at=checked_at,
            )
        except (ContractError, KeyError, TypeError, ValueError, OverflowError):
            return self._build_unavailable_result(
                content_hash,
                reason_code="service_invalid_response",
                retryable=False,
            )

    def _current_cache_lookup(
        self,
        request: dict[str, Any],
        api_version: str,
    ) -> tuple[str, str] | None:
        if not isinstance(self.backend, LocalEngineBackend):
            identity = "remote"
        else:
            identity = self.backend.current_cache_identity()
            if identity is None:
                return None
        return self._make_cache_key(request, api_version, identity), identity

    def _make_cache_key(
        self,
        request: dict[str, Any],
        api_version: str,
        backend_identity: str,
    ) -> str:
        request_payload = {
            "contentHash": request["content_hash"],
            "title": request["title"],
            "type": request["type"],
            "tagIds": request["tag_ids"],
            "basicStatement": request["basic_statement"],
        }
        request_digest = hashlib.sha256(
            json.dumps(
                request_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return (
            f"{_CACHE_SCHEMA_REVISION}:{api_version}:{self._pipeline_identity}:"
            f"{backend_identity}:{request_digest}"
        )

    def _result_cache_key(
        self,
        request: dict[str, Any],
        api_version: str,
        search_result: BackendSearchResult,
    ) -> str | None:
        if search_result.status != "complete":
            return None
        if not isinstance(self.backend, LocalEngineBackend):
            return self._make_cache_key(request, api_version, "remote")
        identity = self.backend.current_cache_identity()
        if identity is None:
            return None
        search_identity = search_result.cache_identity
        if (
            search_identity is None
            or identity != search_identity
        ):
            # 检索后索引已变化，旧快照的判断不能登记到新索引身份下。
            return None
        return self._make_cache_key(request, api_version, identity)

    @staticmethod
    def _build_unavailable_result(
        content_hash: str,
        *,
        reason_code: CompletionReason,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        return build_v2_result(
            content_hash=content_hash,
            candidates=[],
            block_submission=False,
            message="本次未能完成原题检索，请稍后重试并由审题人手工核对。",
            completion=_completion(
                status="unavailable",
                reason_code=reason_code,
                retryable=retryable,
                retry_after_seconds=retry_after_seconds,
            ),
            reuse={"policy": "no-store"},
        )


def _completion(
    *,
    status: str,
    reason_code: CompletionReason,
    retryable: bool,
    retry_after_seconds: int | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "reasonCode": reason_code,
        "retryable": retryable,
    }
    if retry_after_seconds is not None:
        result["retryAfterSeconds"] = retry_after_seconds
    return result


def _pipeline_identity(config: AppConfig) -> str:
    """只把会改变判断的非密钥配置做成摘要，不在缓存键中保留配置原文。"""

    relevant = {
        "backend": config.backend,
        "yuantijiBaseUrl": config.yuantiji_base_url,
        "searchK": config.search_k,
        "rerank": config.use_rerank,
        "minimumSimilarity": config.minimum_similarity,
        "blockThreshold": config.block_threshold,
        "similarityBlockEnabled": config.similarity_block_enabled,
        "llmReviewEnabled": config.llm_review_enabled,
        "llmBaseUrl": config.llm_base_url,
        "llmModel": config.llm_model,
        "llmTopN": config.llm_review_top_n,
        "localVectorTopK": config.local_vector_top_k,
        "localKeywordTopK": config.local_keyword_top_k,
        "embeddingBaseUrl": config.dashscope_base_url,
        "embeddingModel": config.dashscope_embedding_model,
        "embeddingDimensions": config.dashscope_embedding_dim,
    }
    return hashlib.sha256(
        json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _v1_result_from_v2(result: dict[str, Any]) -> dict[str, Any]:
    return build_result(
        content_hash=result["contentHash"],
        candidates=result["candidates"],
        block_submission=result["recommendation"]["blockSubmission"],
        message=result["recommendation"]["message"],
        checked_at=result["checkedAt"],
    )


def make_handler(
    service: AnklangService,
    runtime: ServiceRuntime | None = None,
) -> type[BaseHTTPRequestHandler]:
    runtime = runtime or ServiceRuntime(service.config.max_in_flight_checks)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Anklang/0.1"

        def setup(self) -> None:
            super().setup()
            # socket timeout 是“连续多久没有收到/发出任何字节”的上限，能阻止
            # 声明超长正文后停住的客户端永久占用一个查重名额。
            self.connection.settimeout(service.config.client_idle_timeout_seconds)

        def version_string(self) -> str:
            # 不在 Server 头暴露 Python 运行时版本。
            return self.server_version

        def log_message(self, *_args: Any) -> None:
            # 默认会把整条请求行打印到 stderr，可能含题面片段；这里禁用。
            return

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定的方法名)
            if self.path == _LIVE_PATH:
                self._send(
                    200,
                    {"status": "ok", "service": "anklang", "apiVersion": "1"},
                )
            elif self.path == _READY_PATH:
                self._handle_ready()
            elif self.path == "/api/v1/health":
                self._handle_health()
            elif self.path in {_V1_SIMILARITY_PATH, _V2_SIMILARITY_PATH}:
                self._handle_unsupported_method()
            else:
                self._send(
                    404,
                    {"error": {"code": "NOT_FOUND", "message": "未找到资源。"}},
                )

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle_unsupported_method(suppress_body=True)

        def do_PUT(self) -> None:  # noqa: N802
            self._handle_unsupported_method()

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle_unsupported_method()

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle_unsupported_method()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._handle_unsupported_method()

        def do_TRACE(self) -> None:  # noqa: N802
            self._handle_unsupported_method()

        def do_CONNECT(self) -> None:  # noqa: N802
            self._handle_unsupported_method()

        def send_error(  # type: ignore[override]
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            """替代标准库会回显方法和运行时信息的默认 HTML 错误。"""

            del message, explain
            if code == 501:
                self._send(
                    405,
                    {"error": {"code": "METHOD_NOT_ALLOWED", "message": "请求方法不受支持。"}},
                    suppress_body=getattr(self, "command", "") == "HEAD",
                )
                return
            safe_status = code if 400 <= code <= 599 else 500
            self._send(
                safe_status,
                {"error": {"code": "HTTP_ERROR", "message": "请求无法处理。"}},
                suppress_body=getattr(self, "command", "") == "HEAD",
            )

        def do_POST(self) -> None:  # noqa: N802
            api_version = {
                _V1_SIMILARITY_PATH: "1",
                _V2_SIMILARITY_PATH: "2",
            }.get(self.path)
            if api_version is None:
                self._send(
                    404,
                    {"error": {"code": "NOT_FOUND", "message": "未找到资源。"}},
                )
                return
            # 在鉴权、读取正文和调用任何后端之前取得名额。满载或退出中的请求
            # 都得到同一个小型响应；未读正文所在连接随即关闭，不能被复用。
            if not runtime.try_begin_check():
                self.close_connection = True
                self._send(
                    503,
                    {
                        "error": {
                            "code": "SERVICE_BUSY",
                            "message": "查重服务正忙，请稍后重试。",
                        }
                    },
                    retry_after_seconds=_BUSY_RETRY_AFTER_SECONDS,
                    close_connection=True,
                )
                return
            try:
                self._handle_similarity(api_version)
            finally:
                runtime.finish_check()

        def _handle_similarity(self, api_version: str) -> None:
            if not self._authorized():
                self._send(
                    401,
                    {"error": {"code": "UNAUTHENTICATED", "message": "缺少或无效的令牌。"}},
                )
                return
            payload = self._read_json()
            if payload is None:
                return
            try:
                request = parse_request(payload, expected_api_version=api_version)
            except ContractError:
                self._send(
                    400,
                    {"error": {"code": "INVALID_REQUEST", "message": "请求不符合接口契约。"}},
                )
                return
            try:
                result = service.check_similarity(request, api_version=api_version)
                result = validate_v2_result(result)
            except (ContractError, KeyError, TypeError, ValueError, OverflowError):
                self._send(
                    500,
                    {"error": {"code": "INVALID_RESULT", "message": "服务无法形成可信的查重结果。"}},
                )
                return
            except Exception:
                self._send(
                    503,
                    {"error": {"code": "CHECK_UNAVAILABLE", "message": "查重服务暂时不可用。"}},
                )
                return

            completion = result["completion"]
            retry_after = completion.get("retryAfterSeconds")
            if api_version == "1":
                if completion["status"] != "complete":
                    self._send(
                        503,
                        {
                            "error": {
                                "code": "CHECK_INCOMPLETE",
                                "message": "本次未能完成原题检索，请稍后重试并人工核对。",
                            }
                        },
                        retry_after_seconds=retry_after,
                    )
                    return
                try:
                    legacy_result = _v1_result_from_v2(result)
                except (ContractError, KeyError, TypeError, ValueError, OverflowError):
                    self._send(
                        500,
                        {"error": {"code": "INVALID_RESULT", "message": "服务无法形成可信的查重结果。"}},
                    )
                    return
                self._send(200, legacy_result)
                return
            self._send(
                200,
                result,
                retry_after_seconds=retry_after,
            )

        def _handle_unsupported_method(self, *, suppress_body: bool = False) -> None:
            if self.path in {_V1_SIMILARITY_PATH, _V2_SIMILARITY_PATH}:
                self._send(
                    405,
                    {"error": {"code": "METHOD_NOT_ALLOWED", "message": "请求方法不受支持。"}},
                    suppress_body=suppress_body,
                )
                return
            self._send(
                404,
                {"error": {"code": "NOT_FOUND", "message": "未找到资源。"}},
                suppress_body=suppress_body,
            )

        def _handle_ready(self) -> None:
            """提供方无关的就绪检查：只验证本地服务/配置不变量，不调用任何
            后端、不发起任何网络请求，也不读取题库。与 /api/v1/live（仅存活）
            和 /api/v1/health（会透传上游后端状态）保持语义区分。"""
            if runtime.accepting:
                self._send(
                    200,
                    {"status": "ok", "service": "anklang", "apiVersion": "1", "ready": True},
                )
            else:
                self._send(
                    503,
                    {
                        "status": "not_ready",
                        "service": "anklang",
                        "apiVersion": "1",
                        "ready": False,
                    },
                )

        def _handle_health(self) -> None:
            info: dict[str, Any] = {
                "status": "ok",
                "service": "anklang",
                "apiVersion": "1",
                "backend": service.config.backend,
            }
            # describe_health() 约定不抛异常，各后端把自己的失败情况体现成状态字段
            # （例如 upstreamReady=False），这里统一根据这些字段判断是否整体降级。
            try:
                info.update(service.backend.describe_health())
            except Exception:
                # 健康检查也不传播外部服务的异常原文。
                info["status"] = "degraded"
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
                self._send(
                    400,
                    {"error": {"code": "INVALID_REQUEST", "message": "请求体大小不合法。"}},
                )
                return None
            if length <= 0 or length > _MAX_REQUEST_BYTES:
                self._send(
                    400,
                    {"error": {"code": "INVALID_REQUEST", "message": "请求体大小不合法。"}},
                )
                return None
            try:
                raw = self.rfile.read(length)
            except (TimeoutError, socket.timeout, OSError):
                self.close_connection = True
                self._send(
                    408,
                    {
                        "error": {
                            "code": "CLIENT_TIMEOUT",
                            "message": "等待请求正文超时。",
                        }
                    },
                    close_connection=True,
                )
                return None
            if len(raw) != length:
                self.close_connection = True
                self._send(
                    400,
                    {"error": {"code": "INVALID_REQUEST", "message": "请求正文不完整。"}},
                    close_connection=True,
                )
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                self._send(
                    400,
                    {"error": {"code": "INVALID_REQUEST", "message": "请求体不是有效 JSON。"}},
                )
                return None

        def _send(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            retry_after_seconds: Any = None,
            suppress_body: bool = False,
            close_connection: bool = False,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # Anklang 的任何响应都不应由浏览器或中间代理保存；健康信息和错误
                # 也保持同一条简单、不可被调用方配置绕过的规则。
                self.send_header("Cache-Control", "no-store")
                # 每个响应都带部署修订标识，供发布观测区分版本；由构建/部署流水线
                # 注入（ANKLANG_REVISION），请求时不做任何 Git 或文件系统访问。
                if service.config.revision is not None:
                    self.send_header("X-Anklang-Revision", service.config.revision)
                if close_connection:
                    self.send_header("Connection", "close")
                if (
                    not isinstance(retry_after_seconds, bool)
                    and isinstance(retry_after_seconds, int)
                    and 1 <= retry_after_seconds <= 86_400
                ):
                    self.send_header("Retry-After", str(retry_after_seconds))
                self.end_headers()
                if not suppress_body:
                    self.wfile.write(body)
            except OSError:
                # 客户端中途断开时不输出异常，也不让名额泄漏。
                self.close_connection = True

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


def _install_shutdown_handlers(
    runtime: ServiceRuntime,
) -> dict[int, Any]:
    """只在主线程安装信号处理；返回原处理器以便嵌入测试时恢复。"""

    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}

    def _request_shutdown(_signum: int, _frame: Any) -> None:
        runtime.begin_shutdown()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _request_shutdown)
    return previous


def _restore_shutdown_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def serve(config: AppConfig) -> None:
    service = build_service(config)
    runtime = ServiceRuntime(config.max_in_flight_checks)
    handler = make_handler(service, runtime)
    httpd = AnklangHTTPServer((config.bind_host, config.port), handler)
    # handle_request 的短轮询让信号处理器只改内存状态即可；不需要在信号
    # 处理器中调用 shutdown()，也就不会与同一线程的 serve_forever 死锁。
    httpd.timeout = 0.2
    stop_event = threading.Event()
    ingest_thread: threading.Thread | None = None
    if config.ingest_enabled and isinstance(service.backend, LocalEngineBackend):
        ingest_thread = _start_background_ingest(service.backend, config, stop_event)
    previous_handlers = _install_shutdown_handlers(runtime)
    try:
        while runtime.accepting:
            httpd.handle_request()
    except KeyboardInterrupt:
        runtime.begin_shutdown()
    finally:
        runtime.begin_shutdown()
        stop_event.set()
        # 先关闭监听套接字，再等待已经取得名额的请求。请求线程是 daemon，
        # 即使后端无视自己的超时，也不会越过退出宽限无限阻止进程结束。
        httpd.server_close()
        deadline = time.monotonic() + config.shutdown_grace_seconds
        runtime.wait_for_idle(config.shutdown_grace_seconds)
        if ingest_thread is not None:
            ingest_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        _restore_shutdown_handlers(previous_handlers)
