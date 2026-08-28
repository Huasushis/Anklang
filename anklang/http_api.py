"""带版本号的查询与单题入库 HTTP 适配层。

搜索本身位于保留的上游入口 ``ui/server.py``。本模块只负责 Urmotiv 机器接口的
鉴权、严格 JSON 契约、健康检查和有界关闭；不实现搜索、缓存或产品判断。
"""
from __future__ import annotations

import hmac
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .backends import (
    BackendError,
    BackendUpsertResult,
    CompletionReason,
    SearchBackend,
)
from .config import AppConfig
from .contracts import (
    ContractError,
    build_result,
    build_upsert_result,
    build_v2_result,
    parse_request,
    parse_upsert_request,
    utc_now_z,
    validate_upsert_result,
    validate_v2_result,
)

_MAX_REQUEST_BYTES = 4_000_000
_V1_SIMILARITY_PATH = "/api/v1/checks/similarity"
_V2_SIMILARITY_PATH = "/api/v2/checks/similarity"
_UPSERT_PATH = "/api/v1/index/problems"
_LIVE_PATH = "/api/v1/live"
_READY_PATH = "/api/v1/ready"
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
    ) -> None:
        self.config = config
        self.backend = backend

    def check_similarity(
        self,
        request: dict[str, Any],
        *,
        api_version: str = "2",
    ) -> dict[str, Any]:
        """返回一份经过严格校验的 v2 结果，供两个 HTTP 版本安全投影。"""

        if api_version not in {"1", "2"}:
            raise ValueError("服务端 API 版本不合法。")
        content_hash = request["content_hash"]

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
            candidates = _rank_candidates(self.config, search_result.candidates)
        except (ContractError, KeyError, TypeError, ValueError, OverflowError):
            return self._build_unavailable_result(
                content_hash,
                reason_code="service_invalid_response",
                retryable=False,
            )

        completion = _completion(
            status=search_result.status,
            reason_code=search_result.reason_code,
            retryable=search_result.retryable,
            retry_after_seconds=search_result.retry_after_seconds,
        )
        checked_at = utc_now_z()

        try:
            return build_v2_result(
                content_hash=content_hash,
                candidates=candidates,
                completion=completion,
                checked_at=checked_at,
            )
        except (ContractError, KeyError, TypeError, ValueError, OverflowError):
            return self._build_unavailable_result(
                content_hash,
                reason_code="service_invalid_response",
                retryable=False,
            )

    def upsert_problem(self, request: dict[str, Any]) -> BackendUpsertResult:
        """调用同一运行时索引器写入一道固定来源题目。"""

        indexer = getattr(self.backend, "upsert_problem", None)
        if not callable(indexer):
            raise BackendError(reason_code="service_unavailable", retryable=False)
        try:
            result = indexer(
                request["external_id"],
                request["title"],
                request["basic_statement"],
                request["updated_at"],
            )
        except BackendError:
            raise
        except Exception as error:
            # 后端异常只转换为固定不可用类别，不携带正文、路径或配置细节。
            raise BackendError(
                reason_code="service_unavailable",
                retryable=True,
            ) from error
        if not isinstance(result, BackendUpsertResult):
            raise BackendError(reason_code="service_unavailable", retryable=False)
        return result

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
            completion=_completion(
                status="unavailable",
                reason_code=reason_code,
                retryable=retryable,
                retry_after_seconds=retry_after_seconds,
            ),
        )


def _rank_candidates(
    config: AppConfig,
    raw_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """过滤低于显示下限的候选，并按相似度降序返回。

    是否重复、如何使用候选由调用方决定；Anklang 不形成判定或流程建议。
    """

    visible = [
        dict(candidate)
        for candidate in raw_candidates
        if candidate["similarity"] >= config.minimum_similarity
    ]
    visible.sort(key=lambda candidate: candidate["similarity"], reverse=True)
    return visible


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


def _v1_result_from_v2(result: dict[str, Any]) -> dict[str, Any]:
    return build_result(
        content_hash=result["contentHash"],
        candidates=result["candidates"],
        checked_at=result["checkedAt"],
    )


def make_handler(
    service: AnklangService,
    runtime: ServiceRuntime | None = None,
) -> type[BaseHTTPRequestHandler]:
    active_runtime = runtime or ServiceRuntime(service.config.max_in_flight_checks)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Anklang/0.1"

        def setup(self) -> None:
            super().setup()
            # socket timeout 是"连续多久没有收到/发出任何字节"的上限，能阻止
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
            elif self.path in {
                _V1_SIMILARITY_PATH,
                _V2_SIMILARITY_PATH,
                _UPSERT_PATH,
            }:
                self._handle_unsupported_method()
            else:
                self._send(
                    404,
                    {"error": {"code": "NOT_FOUND", "message": "未找到资源。"}},
                )

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle_unsupported_method(suppress_body=True)

        def do_PUT(self) -> None:  # noqa: N802
            if self.path != _UPSERT_PATH:
                self._handle_unsupported_method()
                return
            # 入库和查重共用同一个在途名额，不能绕过资源上限。
            if not active_runtime.try_begin_check():
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
                self._handle_upsert()
            finally:
                active_runtime.finish_check()

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
                if self.path == _UPSERT_PATH:
                    self._handle_unsupported_method()
                else:
                    self._send(
                        404,
                        {"error": {"code": "NOT_FOUND", "message": "未找到资源。"}},
                    )
                return
            # 在鉴权、读取正文和调用任何后端之前取得名额。满载或退出中的请求
            # 都得到同一个小型响应；未读正文所在连接随即关闭，不能被复用。
            if not active_runtime.try_begin_check():
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
                active_runtime.finish_check()

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
                                "message": "本次未能完成原题检索，请稍后重试。",
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

        def _handle_upsert(self) -> None:
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
                request = parse_upsert_request(payload)
            except ContractError:
                self._send(
                    400,
                    {"error": {"code": "INVALID_REQUEST", "message": "请求不符合接口契约。"}},
                )
                return

            try:
                result = service.upsert_problem(request)
            except Exception:  # noqa: BLE001 - fixed public error boundary
                self._send(
                    503,
                    {
                        "error": {
                            "code": "INDEX_UNAVAILABLE",
                            "message": "题目索引暂时不可用。",
                        }
                    },
                )
                return
            if result.outcome == "stale":
                self._send(
                    409,
                    {
                        "error": {
                            "code": "STALE_UPDATE",
                            "message": "题目版本已过期或发生冲突。",
                        }
                    },
                )
                return
            try:
                response = build_upsert_result(
                    request_id=request["request_id"],
                    external_id=request["external_id"],
                    content_hash=result.content_hash,
                    outcome=result.outcome,
                )
                response = validate_upsert_result(response)
            except (ContractError, KeyError, TypeError, ValueError, OverflowError):
                self._send(
                    503,
                    {
                        "error": {
                            "code": "INDEX_UNAVAILABLE",
                            "message": "题目索引暂时不可用。",
                        }
                    },
                )
                return
            self._send(200, response)

        def _handle_unsupported_method(self, *, suppress_body: bool = False) -> None:
            if self.path in {_V1_SIMILARITY_PATH, _V2_SIMILARITY_PATH, _UPSERT_PATH}:
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
            """就绪检查：只验证本地服务/配置不变量，不调用任何后端、不发起任何
            网络请求，也不读取题库。"""
            if active_runtime.accepting:
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
            }
            # describe_health() returns fixed local status fields. This adapter never calls
            # the embedding provider or includes its exception text.
            try:
                info.update(service.backend.describe_health())
            except Exception:
                # 健康检查也不传播外部服务的异常原文。
                info["status"] = "degraded"
            if (
                info.get("localStoreReady") is False
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
