"""部署可观测性契约测试：修订响应头与提供方无关的就绪检查。

全部使用回环连接和合成正文；不发起任何外部网络请求，也不读取私有数据。
"""
from __future__ import annotations

import json
import os
import threading
import unittest
import yaml
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from anklang.backends import BackendSearchResult
from anklang.cache import ResultCache
from anklang.config import AppConfig, ConfigError
from anklang.server import AnklangService, ServiceRuntime, make_handler


def _config(**overrides: Any) -> AppConfig:
    values = dict(
        port=8730,
        service_token="service-token-abcdef123456",
        yuantiji_base_url="https://yuantiji.test",
        yuantiji_timeout_seconds=12.0,
        yuantiji_minimum_interval_seconds=0.0,
        search_k=8,
        use_rerank=False,
        minimum_similarity=0.5,
        block_threshold=0.9,
        cache_ttl_seconds=3600,
        cache_max_entries=100,
        llm_review_enabled=False,
        llm_base_url=None,
        llm_api_key=None,
        llm_model="synthetic-review-model",
        llm_review_top_n=2,
        llm_timeout_seconds=10.0,
    )
    values.update(overrides)
    return AppConfig(**values)


class _NoCallBackend:
    """一个搜索后端桩：记录所有调用，永远不主动发起网络请求。"""

    def __init__(self) -> None:
        self.search_calls = 0
        self.health_calls = 0
        self.describe_health_calls = 0

    def search(self, _query_text: str, _k: int) -> BackendSearchResult:
        self.search_calls += 1
        return BackendSearchResult([])

    def describe_health(self) -> dict[str, Any]:
        self.describe_health_calls += 1
        return {"upstreamReady": True}


class _Harness:
    def __init__(
        self,
        backend: _NoCallBackend,
        config: AppConfig | None = None,
    ) -> None:
        self.config = config or _config()
        self.runtime = ServiceRuntime(self.config.max_in_flight_checks)
        self.service = AnklangService(
            self.config,
            backend,
            ResultCache(
                self.config.cache_ttl_seconds,
                self.config.cache_max_entries,
            ),
            None,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, self.runtime)
        )
        self.port = int(self.server.server_address[1])
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self._thread.start()

    def get(self, path: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            raw = response.read()
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
            return (
                response.status,
                decoded,
                {name.lower(): value for name, value in response.getheaders()},
            )
        finally:
            connection.close()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | str | None = None,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if isinstance(body, str):
            body = body.encode("utf-8")
        connection = HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
            return (
                response.status,
                decoded,
                {name.lower(): value for name, value in response.getheaders()},
            )
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RevisionHeaderTests(unittest.TestCase):
    """修订标识 X-Anklang-Revision 在成功和错误响应上都必须出现。"""

    def test_revision_header_on_live_success(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision="abc1234"))
        self.addCleanup(harness.close)
        status, payload, headers = harness.get("/api/v1/live")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-anklang-revision"], "abc1234")

    def test_revision_header_on_404_error(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision="v1.2.3-rc4"))
        self.addCleanup(harness.close)
        status, payload, headers = harness.get("/api/v1/nonexistent")
        self.assertEqual(status, 404)
        self.assertEqual(headers["x-anklang-revision"], "v1.2.3-rc4")

    def test_revision_header_on_ready_success(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision="deadbeef"))
        self.addCleanup(harness.close)
        status, payload, headers = harness.get("/api/v1/ready")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-anklang-revision"], "deadbeef")

    def test_no_revision_header_when_not_set(self) -> None:
        """revision 留空时不输出 X-Anklang-Revision 头（确定性缺失策略）。"""
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision=None))
        self.addCleanup(harness.close)
        status, _payload, headers = harness.get("/api/v1/live")
        self.assertEqual(status, 200)
        self.assertNotIn("x-anklang-revision", headers)

    def test_revision_header_consistent_across_responses(self) -> None:
        """成功和错误响应的修订标识必须一致。"""
        backend = _NoCallBackend()
        harness = _Harness(backend, _config(revision="consist-rev"))
        self.addCleanup(harness.close)
        _s1, _p1, h1 = harness.get("/api/v1/live")
        _s2, _p2, h2 = harness.get("/api/v1/ready")
        _s3, _p3, h3 = harness.get("/api/v1/bad")
        self.assertEqual(h1["x-anklang-revision"], "consist-rev")
        self.assertEqual(h2["x-anklang-revision"], "consist-rev")
        self.assertEqual(h3["x-anklang-revision"], "consist-rev")


class RevisionValidationTests(unittest.TestCase):
    """ANKLANG_REVISION 的确定性格式校验。"""

    def test_valid_revision(self) -> None:
        with patch.dict(os.environ, {"ANKLANG_REVISION": "abc1234"}, clear=False):
            config = _config(revision="abc1234")
            self.assertEqual(config.revision, "abc1234")

    def test_empty_revision_means_none(self) -> None:
        with patch.dict(os.environ, {"ANKLANG_REVISION": ""}, clear=False):
            from anklang.config import _read_revision

            self.assertIsNone(_read_revision("ANKLANG_REVISION"))

    def test_invalid_revision_rejected(self) -> None:
        """包含空格、斜杠、特殊字符的修订标识被拒绝。"""
        from anklang.config import _REVISION_RE, _read_revision

        # 控制字符无法通过 os.environ 设置（OS 限制），直接验证正则不会匹配。
        self.assertIsNone(_REVISION_RE.fullmatch("abc\x00def"))

        for bad_value in (
            "abc 1234",
            "abc/1234",
            "abc;1234",
            "abc'1234",
            "abc\"1234",
            "../etc/passwd",
            "a" * 201,  # 超过 200 字符
        ):
            with self.subTest(bad_value=bad_value):
                with patch.dict(
                    os.environ, {"ANKLANG_REVISION": bad_value}, clear=False
                ):
                    with self.assertRaises(ConfigError):
                        _read_revision("ANKLANG_REVISION")

    def test_valid_characters_accepted(self) -> None:
        """字母、数字、点、下划线和连字符都被接受。"""
        from anklang.config import _read_revision

        for good_value in (
            "abc1234",
            "v1.2.3",
            "feature_branch",
            "rev-2026-08-13",
            "a" * 200,  # 恰好 200 字符
        ):
            with self.subTest(good_value=good_value):
                with patch.dict(
                    os.environ, {"ANKLANG_REVISION": good_value}, clear=False
                ):
                    result = _read_revision("ANKLANG_REVISION")
                    self.assertEqual(result, good_value)


class ReadinessEndpointTests(unittest.TestCase):
    """/api/v1/ready 是提供方无关的就绪检查。"""

    def test_ready_returns_200_when_accepting(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)
        status, payload, headers = harness.get("/api/v1/ready")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "anklang")
        self.assertEqual(payload["apiVersion"], "1")
        self.assertTrue(payload["ready"])

    def test_ready_returns_503_when_not_accepting(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)
        harness.runtime.begin_shutdown()
        status, payload, _headers = harness.get("/api/v1/ready")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["ready"])

    def test_ready_makes_zero_backend_or_health_calls(self) -> None:
        """就绪检查不调用后端搜索，也不调用 describe_health。"""
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)
        harness.get("/api/v1/ready")
        self.assertEqual(backend.search_calls, 0)
        self.assertEqual(backend.describe_health_calls, 0)

    def test_ready_returns_no_store_cache_control(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)
        _status, _payload, headers = harness.get("/api/v1/ready")
        self.assertEqual(headers["cache-control"], "no-store")

    def test_ready_distinct_from_live_and_health(self) -> None:
        """/api/v1/ready 只验证本地状态，不透传上游；/api/v1/health 调用 describe_health。"""
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)

        _s_ready, _p_ready, _h_ready = harness.get("/api/v1/ready")
        self.assertEqual(backend.describe_health_calls, 0)

        _s_live, _p_live, _h_live = harness.get("/api/v1/live")
        self.assertEqual(backend.describe_health_calls, 0)

        _s_health, _p_health, _h_health = harness.get("/api/v1/health")
        self.assertEqual(backend.describe_health_calls, 1)


class NoBackendTransportProofTests(unittest.TestCase):
    """证明 /api/v1/ready 不发起任何后端/网络传输。"""

    def test_ready_does_not_invoke_backend_search_method(self) -> None:
        backend = _NoCallBackend()
        harness = _Harness(backend, _config())
        self.addCleanup(harness.close)

        # 多次调用 ready，确认后端搜索方法从未被调用
        for _ in range(3):
            harness.get("/api/v1/ready")

        self.assertEqual(backend.search_calls, 0)
        self.assertEqual(backend.describe_health_calls, 0)

    def test_ready_works_even_if_backend_would_raise(self) -> None:
        """即使后端的 search 方法会抛异常，ready 仍然成功返回。"""

        class _ExplodingBackend:
            def search(self, _q: str, _k: int) -> BackendSearchResult:
                raise RuntimeError("backend should not be called")

            def describe_health(self) -> dict[str, Any]:
                raise RuntimeError("health should not be called")

        backend = _ExplodingBackend()
        config = _config()
        runtime = ServiceRuntime(config.max_in_flight_checks)
        service = AnklangService(
            config,
            backend,  # type: ignore[arg-type]
            ResultCache(config.cache_ttl_seconds, config.cache_max_entries),
            None,
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(service, runtime)
        )
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/api/v1/ready")
                response = connection.getresponse()
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["ready"])
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()

class LoadConfigWiringTests(unittest.TestCase):
    """load_config() 将 ANKLANG_REVISION 环境变量正确映射到 AppConfig.revision。"""

    def test_revision_unset_means_none(self) -> None:
        """ANKLANG_REVISION 未设置时 load_config 返回 revision=None。"""
        from anklang.config import load_config

        env = {k: v for k, v in os.environ.items() if k != "ANKLANG_REVISION"}
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        self.assertIsNone(config.revision)

    def test_revision_empty_means_none(self) -> None:
        """ANKLANG_REVISION 设为空字符串时 load_config 返回 revision=None。"""
        from anklang.config import load_config

        with patch.dict(os.environ, {"ANKLANG_REVISION": ""}, clear=True):
            config = load_config()
        self.assertIsNone(config.revision)

    def test_revision_set_means_value(self) -> None:
        """ANKLANG_REVISION 设为有效值时 load_config 返回该值。"""
        from anklang.config import load_config

        with patch.dict(
            os.environ, {"ANKLANG_REVISION": "v2.0.1-rc3"}, clear=True
        ):
            config = load_config()
        self.assertEqual(config.revision, "v2.0.1-rc3")

    def test_revision_invalid_raises_config_error(self) -> None:
        """ANKLANG_REVISION 包含非法字符时 load_config 抛出 ConfigError。"""
        from anklang.config import load_config

        with patch.dict(
            os.environ, {"ANKLANG_REVISION": "bad/revision"}, clear=True
        ):
            with self.assertRaises(ConfigError):
                load_config()

    def test_revision_too_long_raises_config_error(self) -> None:
        """ANKLANG_REVISION 超过 200 字符时 load_config 抛出 ConfigError。"""
        from anklang.config import load_config

        with patch.dict(
            os.environ, {"ANKLANG_REVISION": "a" * 201}, clear=True
        ):
            with self.assertRaises(ConfigError):
                load_config()

    def test_revision_whitespace_only_means_none(self) -> None:
        """ANKLANG_REVISION 只含空白时 load_config 返回 revision=None。"""
        from anklang.config import load_config

        with patch.dict(os.environ, {"ANKLANG_REVISION": "  "}, clear=True):
            config = load_config()
        self.assertIsNone(config.revision)


class RevisionHeaderOnErrorPathsTests(unittest.TestCase):
    """X-Anklang-Revision 出现在 401/400/503/405/408 等错误路径的响应上。"""

    _REVISION = "err-rev-001"
    _TOKEN = "service-token-abcdef123456"
    _V1 = "/api/v1/checks/similarity"
    _V2 = "/api/v2/checks/similarity"

    def _make_harness(self, **overrides: Any) -> _Harness:
        backend = _NoCallBackend()
        cfg = _config(revision=self._REVISION, **overrides)
        return _Harness(backend, cfg)

    def test_header_on_401_unauthenticated(self) -> None:
        """缺少令牌的 POST 返回 401 且带 X-Anklang-Revision。"""
        harness = self._make_harness()
        self.addCleanup(harness.close)
        status, _payload, headers = harness.request(
            "POST",
            self._V2,
            body=json.dumps({"apiVersion": "2", "problems": []}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(headers["x-anklang-revision"], self._REVISION)

    def test_header_on_400_invalid_request(self) -> None:
        """请求正文不符合契约时返回 400 且带 X-Anklang-Revision。"""
        harness = self._make_harness()
        self.addCleanup(harness.close)
        status, _payload, headers = harness.request(
            "POST",
            self._V2,
            body=json.dumps({"apiVersion": "2", "problems": "not-a-list"}),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._TOKEN}",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(headers["x-anklang-revision"], self._REVISION)

    def test_header_on_400_bad_content_length(self) -> None:
        """Content-Length 非法时返回 400 且带 X-Anklang-Revision。"""
        harness = self._make_harness()
        self.addCleanup(harness.close)
        status, _payload, headers = harness.request(
            "POST",
            self._V2,
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "not-a-number",
                "Authorization": f"Bearer {self._TOKEN}",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(headers["x-anklang-revision"], self._REVISION)

    def test_header_on_503_service_busy(self) -> None:
        """满载时返回 503 且带 X-Anklang-Revision。"""
        harness = self._make_harness(max_in_flight_checks=1)
        self.addCleanup(harness.close)
        # 占满唯一的并发名额
        harness.runtime._in_flight = 1  # type: ignore[attr-defined]
        status, _payload, headers = harness.request(
            "POST",
            self._V2,
            body=json.dumps({"apiVersion": "2", "problems": []}),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._TOKEN}",
            },
        )
        self.assertEqual(status, 503)
        self.assertEqual(headers["x-anklang-revision"], self._REVISION)

    def test_header_on_405_method_not_allowed(self) -> None:
        """不允许的方法返回 405 且带 X-Anklang-Revision。"""
        harness = self._make_harness()
        self.addCleanup(harness.close)
        status, _payload, headers = harness.request(
            "PUT",
            self._V1,
            body=b"",
        )
        self.assertEqual(status, 405)
        self.assertEqual(headers["x-anklang-revision"], self._REVISION)

    def test_header_on_408_client_timeout(self) -> None:
        """请求正文超时返回 408 且带 X-Anklang-Revision。

        使用极短的 client_idle_timeout_seconds，通过原始 socket 发送
        Content-Length 但不发送正文，触发服务端读取超时。
        """
        import socket

        harness = self._make_harness(client_idle_timeout_seconds=0.3)
        self.addCleanup(harness.close)
        sock = socket.create_connection(("127.0.0.1", harness.port), timeout=5)
        try:
            # 发送 POST 请求头，声明 Content-Length: 100 但不发送正文
            request_line = (
                f"POST {self._V2} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{harness.port}\r\n"
                "Authorization: Bearer " + self._TOKEN + "\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 100\r\n"
                "\r\n"
            )
            sock.sendall(request_line.encode("utf-8"))
            # 等待服务端超时后返回 408
            sock.settimeout(5)
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
        finally:
            sock.close()
        # 解析状态行和响应头
        self.assertTrue(raw, "server did not send a 408 response")
        status_line = raw.split(b"\r\n", 1)[0].decode("ascii")
        self.assertIn("408", status_line)
        header_block = raw.split(b"\r\n\r\n", 1)[0]
        header_text = header_block.decode("utf-8")
        self.assertIn("X-Anklang-Revision: " + self._REVISION, header_text)

class ComposeEnvFileRevisionTests(unittest.TestCase):
    """证明随附的 Compose + 示例环境文件默认不会用空值覆盖构建注入的修订标识。

    部署修订标识的来源优先级：
    1. private/anklang.env 中显式设置的非空值（运行时覆盖，优先于镜像 ENV）
    2. Dockerfile 构建参数注入的镜像 ENV（可靠默认值）
    3. 均未设置时 revision=None，不输出 X-Anklang-Revision 头

    随附的 .env.example 必须不主动定义 ANKLANG_REVISION（处于注释状态），
    这样操作者直接复制到 private/anklang.env 时不会用空值覆盖构建注入。
    """

    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_env_example_does_not_define_revision_by_default(self) -> None:
        """随附的 .env.example 中 ANKLANG_REVISION 处于注释状态，不是活跃赋值。"""
        example_path = os.path.join(self._REPO_ROOT, ".env.example")
        with open(example_path, encoding="utf-8") as f:
            lines = f.readlines()
        active = [
            line.strip()
            for line in lines
            if line.strip()
            and not line.strip().startswith("#")
            and "ANKLANG_REVISION=" in line
        ]
        self.assertEqual(
            active,
            [],
            ".env.example 不得包含未注释的 ANKLANG_REVISION= 赋值，"
 "否则直接复制到 private/anklang.env 会用空值覆盖构建注入的修订标识",
        )

    def test_env_example_documents_commented_override(self) -> None:
        """.env.example 包含注释形式的 ANKLANG_REVISION 覆盖说明。"""
        example_path = os.path.join(self._REPO_ROOT, ".env.example")
        with open(example_path, encoding="utf-8") as f:
            content = f.read()
        # 确认存在注释行的 ANKLANG_REVISION，供操作者取消注释使用
        commented = [
            line for line in content.splitlines()
            if line.strip().startswith("#ANKLANG_REVISION=")
        ]
        self.assertTrue(
            commented,
            ".env.example 应包含注释形式的 #ANKLANG_REVISION= 供运行时覆盖",
        )

    def test_compose_environment_does_not_define_revision(self) -> None:
        """compose.yaml 的 environment 块不定义 ANKLANG_REVISION。"""
        compose_path = os.path.join(self._REPO_ROOT, "compose.yaml")
        with open(compose_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        env_block = (
            data.get("services", {})
            .get("anklang", {})
            .get("environment", {})
        )
        self.assertNotIn(
            "ANKLANG_REVISION",
            env_block,
            "compose.yaml environment 块不得定义 ANKLANG_REVISION，"
 "否则空插值会覆盖镜像构建注入的修订标识",
        )

    def test_compose_build_args_still_passes_revision(self) -> None:
        """compose.yaml 的 build.args 仍然传递 ANKLANG_REVISION 给 Dockerfile。"""
        compose_path = os.path.join(self._REPO_ROOT, "compose.yaml")
        with open(compose_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        build_args = (
            data.get("services", {})
            .get("anklang", {})
            .get("build", {})
            .get("args", {})
        )
        self.assertIn(
            "ANKLANG_REVISION",
            build_args,
            "compose.yaml build.args 必须传递 ANKLANG_REVISION 给 Dockerfile",
        )

    def test_dockerfile_sets_env_from_arg(self) -> None:
        """Dockerfile 通过 ARG + ENV 将构建参数写入镜像环境变量。"""
        dockerfile_path = os.path.join(self._REPO_ROOT, "Dockerfile")
        with open(dockerfile_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("ARG ANKLANG_REVISION=", content)
        self.assertIn("ENV ANKLANG_REVISION=", content)

    def test_verbatim_env_file_does_not_shadow_build_revision(self) -> None:
        """模拟操作者直接复制 .env.example：解析后的活跃变量不含 ANKLANG_REVISION。

        Docker Compose 的 env_file 只读取未注释的 KEY=VALUE 行；
        注释行（以 # 开头）被忽略。因此注释状态的 #ANKLANG_REVISION= 不会
        向容器注入空值，构建注入的镜像 ENV 保持有效。
        """
        example_path = os.path.join(self._REPO_ROOT, ".env.example")
        with open(example_path, encoding="utf-8") as f:
            lines = f.readlines()
        # 模拟 Compose env_file 解析：只保留未注释的 KEY=VALUE 行
        parsed: dict[str, str] = {}
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                key, _, value = stripped.partition("=")
                parsed[key.strip()] = value
        self.assertNotIn(
            "ANKLANG_REVISION",
            parsed,
            "直接复制 .env.example 后解析的活跃变量不得包含 ANKLANG_REVISION",
        )

    def test_explicit_nonempty_runtime_override_wins(self) -> None:
        """操作者在 private/anklang.env 中显式设置非空值时，该值生效。

        模拟 env_file 包含 ANKLANG_REVISION=override-123 的场景：
        load_config 应返回该值，覆盖镜像构建注入的默认值。
        """
        from anklang.config import load_config

        with patch.dict(
            os.environ,
            {"ANKLANG_REVISION": "override-123"},
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.revision, "override-123")

    def test_empty_runtime_override_means_no_header(self) -> None:
        """即使操作者显式设了空值，load_config 返回 None（不输出头）。

        这是安全行为：空值不泄露路径或密钥，也不输出无效修订标识。
        """
        from anklang.config import load_config

        with patch.dict(
            os.environ, {"ANKLANG_REVISION": ""}, clear=True
        ):
            config = load_config()
        self.assertIsNone(config.revision)


if __name__ == "__main__":
    unittest.main()
