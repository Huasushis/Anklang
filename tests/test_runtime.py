"""生产 HTTP 运行时边界；只使用回环连接和合成正文。"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from anklang.__main__ import main
from anklang.backends import BackendSearchResult

from anklang.config import AppConfig
from anklang.http_api import (
    AnklangHTTPServer,
    AnklangService,
    ServiceRuntime,
    make_handler,
)


def _config(**overrides: Any) -> AppConfig:
    values = dict(
        port=8730,
        service_token="synthetic-service-token",
        search_k=8,
        minimum_similarity=0.1,
        max_in_flight_checks=1,
        client_idle_timeout_seconds=0.15,
        shutdown_grace_seconds=0.15,
    )
    values.update(overrides)
    return AppConfig(**values)


def _payload() -> bytes:
    return json.dumps(
        {
            "apiVersion": "2",
            "requestId": "66666666-6666-4666-8666-666666666666",
            "contentHash": "6" * 64,
            "problem": {
                "title": "合成运行时测试",
                "type": "traditional",
                "tagIds": ["synthetic"],
                "basicStatement": "合成正文 alpha beta",
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


class _Backend:
    def __init__(self, *, blocking: bool = False) -> None:
        self.blocking = blocking
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.health_calls = 0

    def search(self, _query_text: str, _k: int) -> BackendSearchResult:
        self.calls += 1
        self.entered.set()
        if self.blocking:
            self.release.wait(5.0)
        return BackendSearchResult([])

    def describe_health(self) -> dict[str, Any]:
        self.health_calls += 1
        return {"upstreamReady": True}


class _Harness:
    def __init__(self, backend: _Backend, config: AppConfig | None = None) -> None:
        self.config = config or _config()
        self.runtime = ServiceRuntime(self.config.max_in_flight_checks)
        self.service = AnklangService(
            self.config,
            backend,
        )
        self.server = AnklangHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, self.runtime)
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def post(self) -> tuple[int, dict[str, Any], dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            body = _payload()
            connection.request(
                "POST",
                "/api/v2/checks/similarity",
                body=body,
                headers={
                    "Authorization": "Bearer synthetic-service-token",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            raw = response.read()
            return (
                response.status,
                json.loads(raw.decode("utf-8")),
                {name.lower(): value for name, value in response.getheaders()},
            )
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _raw_exchange(port: int, request_prefix: bytes, *, timeout: float = 2.0) -> bytes:
    connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        connection.settimeout(timeout)
        connection.sendall(request_prefix)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65_536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        connection.close()


def _partial_post_headers(content_length: int) -> bytes:
    return (
        "POST /api/v2/checks/similarity HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Authorization: Bearer synthetic-service-token\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {content_length}\r\n"
        "\r\n"
    ).encode("ascii")


class ServiceRuntimeTests(unittest.TestCase):
    def test_capacity_rejects_non_positive_and_non_integer_values(self) -> None:
        for value in (0, -1, True, 1.5, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ServiceRuntime(value)  # type: ignore[arg-type]

    def test_startup_failure_hides_original_exception(self) -> None:
        private_marker = "synthetic-private-startup-detail"
        stderr = io.StringIO()
        with patch("anklang.__main__.load_config", return_value=_config()), patch(
            "anklang.__main__.serve", side_effect=RuntimeError(private_marker)
        ), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(), 1)
        output = stderr.getvalue()
        self.assertNotIn(private_marker, output)
        self.assertNotIn("Traceback", output)
        self.assertIn("启动失败", output)

    def test_capacity_shutdown_and_bounded_wait_are_atomic(self) -> None:
        runtime = ServiceRuntime(1)
        self.assertTrue(runtime.try_begin_check())
        self.assertFalse(runtime.try_begin_check())
        started = time.monotonic()
        self.assertFalse(runtime.wait_for_idle(0.02))
        self.assertLess(time.monotonic() - started, 0.5)
        runtime.begin_shutdown()
        runtime.finish_check()
        self.assertTrue(runtime.wait_for_idle(0.02))
        self.assertFalse(runtime.try_begin_check())
        self.assertEqual(runtime.in_flight, 0)

    def test_full_capacity_rejects_before_reading_body_or_calling_backend(self) -> None:
        backend = _Backend(blocking=True)
        harness = _Harness(backend)
        self.addCleanup(backend.release.set)
        self.addCleanup(harness.close)
        first_result: list[tuple[int, dict[str, Any], dict[str, str]]] = []
        first = threading.Thread(target=lambda: first_result.append(harness.post()))
        first.start()
        self.assertTrue(backend.entered.wait(2.0))

        started = time.monotonic()
        raw = _raw_exchange(harness.port, _partial_post_headers(1_000_000))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertIn(b" 503 ", raw.split(b"\r\n", 1)[0])
        self.assertIn(b'"code": "SERVICE_BUSY"', raw)
        self.assertIn(b"Cache-Control: no-store", raw)
        self.assertIn(b"Retry-After: 1", raw)
        self.assertIn(b"Connection: close", raw)
        self.assertEqual(backend.calls, 1)

        backend.release.set()
        first.join(2.0)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_result[0][0], 200)
        self.assertEqual(harness.runtime.in_flight, 0)

    def test_shutdown_rejects_before_body_and_live_does_not_use_a_slot(self) -> None:
        backend = _Backend()
        harness = _Harness(backend)
        self.addCleanup(harness.close)
        harness.runtime.begin_shutdown()

        raw = _raw_exchange(harness.port, _partial_post_headers(1_000_000))
        self.assertIn(b" 503 ", raw.split(b"\r\n", 1)[0])
        self.assertIn(b'"code": "SERVICE_BUSY"', raw)
        self.assertEqual(backend.calls, 0)

        connection = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=2)
        try:
            connection.request("GET", "/api/v1/live")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
        finally:
            connection.close()
        self.assertEqual(backend.health_calls, 0)

    def test_slow_body_times_out_releases_slot_and_next_request_succeeds(self) -> None:
        backend = _Backend()
        harness = _Harness(backend)
        self.addCleanup(harness.close)
        raw = _raw_exchange(
            harness.port,
            _partial_post_headers(100) + b"{",
            timeout=2.0,
        )
        self.assertIn(b" 408 ", raw.split(b"\r\n", 1)[0])
        self.assertIn(b'"code": "CLIENT_TIMEOUT"', raw)
        self.assertIn(b"Cache-Control: no-store", raw)
        self.assertEqual(harness.runtime.in_flight, 0)
        self.assertEqual(backend.calls, 0)

        status, _payload_result, headers = harness.post()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(backend.calls, 1)


@unittest.skipUnless(hasattr(signal, "SIGTERM"), "需要 POSIX SIGTERM")
class ProcessSignalTests(unittest.TestCase):
    def test_sigterm_stops_listener_and_exits_within_grace(self) -> None:
        self._assert_signal_exit(signal.SIGTERM)

    def test_sigint_stops_listener_and_exits_within_grace(self) -> None:
        self._assert_signal_exit(signal.SIGINT)

    def _assert_signal_exit(self, shutdown_signal: int) -> None:
        repository = Path(__file__).resolve().parents[1]
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
        probe.close()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(repository),
            "ANKLANG_BIND_HOST": "127.0.0.1",
            "ANKLANG_PORT": str(port),
            "ANKLANG_SHUTDOWN_GRACE_SECONDS": "1",
            "ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS": "1",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "anklang"],
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop_child, process)
        deadline = time.monotonic() + 5.0
        while True:
            if process.poll() is not None:
                stderr = self._read_child_stderr(process)
                self.fail(f"子进程在监听前退出：{stderr}")
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", port, timeout=0.2
                )
                connection.request("GET", "/api/v1/live")
                response = connection.getresponse()
                response.read()
                connection.close()
                if response.status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                self.fail("Anklang 子进程未在期限内开始监听。")
            time.sleep(0.02)

        started = time.monotonic()
        process.send_signal(shutdown_signal)
        return_code = process.wait(timeout=3.0)
        elapsed = time.monotonic() - started
        stderr = self._read_child_stderr(process)
        self.assertEqual(return_code, 0)
        self.assertLess(elapsed, 2.5)
        self.assertNotIn("Traceback", stderr)

        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)

    @staticmethod
    def _stop_child(process: subprocess.Popen[bytes]) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
        finally:
            if process.stderr is not None and not process.stderr.closed:
                process.stderr.close()

    @staticmethod
    def _read_child_stderr(process: subprocess.Popen[bytes]) -> str:
        if process.stderr is None:
            return ""
        try:
            return process.stderr.read().decode("utf-8", "replace")
        finally:
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
