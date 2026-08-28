"""环境变量读取测试；不联网，也不读取开发机器的真实环境变量。"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from anklang.config import ConfigError, _read_bool, load_config


class BooleanConfigTests(unittest.TestCase):
    def test_boolean_reader_accepts_only_empty_true_and_false(self) -> None:
        for raw, expected in (("", False), ("true", True), ("false", False)):
            with self.subTest(raw=raw), patch.dict(
                os.environ, {"TEST_BOOLEAN": raw}, clear=True
            ):
                self.assertIs(_read_bool("TEST_BOOLEAN"), expected)

        with patch.dict(os.environ, {"TEST_BOOLEAN": ""}, clear=True):
            self.assertTrue(_read_bool("TEST_BOOLEAN", default=True))

    def test_every_boolean_setting_uses_strict_reader(self) -> None:
        fields = (
            ("ANKLANG_INGEST_ENABLED", "ingest_enabled"),
            ("ANKLANG_REQUIRE_SERVICE_TOKEN", "require_service_token"),
        )
        for variable, field in fields:
            environment = {variable: "true"}
            if variable == "ANKLANG_REQUIRE_SERVICE_TOKEN":
                environment["ANKLANG_SERVICE_TOKEN"] = "synthetic-token-123456"
            with self.subTest(variable=variable), patch.dict(
                os.environ, environment, clear=True
            ):
                self.assertTrue(getattr(load_config(), field))

    def test_boolean_typo_is_rejected_without_echoing_it(self) -> None:
        private_marker = "truthy-private-marker"
        with patch.dict(
            os.environ, {"ANKLANG_INGEST_ENABLED": private_marker}, clear=True
        ):
            with self.assertRaises(ConfigError) as raised:
                load_config()
            self.assertNotIn(private_marker, str(raised.exception))
            self.assertIn("true/false", str(raised.exception))


class RuntimeConfigTests(unittest.TestCase):
    def test_runtime_defaults_are_private_and_bounded(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertFalse(config.require_service_token)
        self.assertEqual(config.max_in_flight_checks, 16)
        self.assertEqual(config.client_idle_timeout_seconds, 15.0)
        self.assertEqual(config.shutdown_grace_seconds, 30.0)

    def test_explicit_container_bind_host_is_accepted(self) -> None:
        for host in ("0.0.0.0", "127.0.0.1", "localhost"):
            with self.subTest(host=host), patch.dict(
                os.environ, {"ANKLANG_BIND_HOST": host}, clear=True
            ):
                self.assertEqual(load_config().bind_host, host)

    def test_bind_host_rejects_url_port_and_control_forms_without_echo(self) -> None:
        private_marker = "private-host-marker"
        for host in (
            "http://127.0.0.1",
            "127.0.0.1:8730",
            "::1",
            "bad host",
            f"{private_marker}/path",
        ):
            with self.subTest(host=host), patch.dict(
                os.environ, {"ANKLANG_BIND_HOST": host}, clear=True
            ):
                with self.assertRaises(ConfigError) as raised:
                    load_config()
                self.assertNotIn(private_marker, str(raised.exception))

    def test_required_service_token_must_exist_and_be_long_enough(self) -> None:
        private_marker = "short-secret"
        for environment in (
            {"ANKLANG_REQUIRE_SERVICE_TOKEN": "true"},
            {
                "ANKLANG_REQUIRE_SERVICE_TOKEN": "true",
                "ANKLANG_SERVICE_TOKEN": private_marker,
            },
        ):
            with self.subTest(environment=tuple(environment)), patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaises(ConfigError) as raised:
                    load_config()
                self.assertNotIn(private_marker, str(raised.exception))

        with patch.dict(
            os.environ,
            {
                "ANKLANG_REQUIRE_SERVICE_TOKEN": "true",
                "ANKLANG_SERVICE_TOKEN": "synthetic-token-123456",
            },
            clear=True,
        ):
            config = load_config()
        self.assertTrue(config.require_service_token)
        self.assertEqual(config.service_token, "synthetic-token-123456")

    def test_runtime_numeric_settings_enforce_closed_ranges(self) -> None:
        cases = (
            ("ANKLANG_MAX_IN_FLIGHT_CHECKS", "1", "256", "0", "257"),
            ("ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS", "1", "300", "0.99", "300.01"),
            ("ANKLANG_SHUTDOWN_GRACE_SECONDS", "1", "300", "0.99", "300.01"),
        )
        for variable, minimum, maximum, below, above in cases:
            for accepted in (minimum, maximum):
                with self.subTest(variable=variable, accepted=accepted), patch.dict(
                    os.environ, {variable: accepted}, clear=True
                ):
                    load_config()
            for rejected in (below, above, "not-a-number"):
                with self.subTest(variable=variable, rejected=rejected), patch.dict(
                    os.environ, {variable: rejected}, clear=True
                ):
                    with self.assertRaises(ConfigError):
                        load_config()


class DashScopeInertnessTests(unittest.TestCase):
    """DASHSCOPE_* 环境变量不再被读取：无论取值是否合法，都不能激活或拒绝启动。"""

    def test_dashscope_env_is_completely_ignored(self) -> None:
        for raw in (
            "https://dashscope.example.invalid/compatible-mode/v1",
            "ftp://example.com",
            "javascript:alert(1)",
            "https://exa%mple.invalid",
        ):
            with self.subTest(raw=raw), patch.dict(
                os.environ,
                {
                    "DASHSCOPE_BASE_URL": raw,
                    "DASHSCOPE_API_KEY": "synthetic-embedding-key",
                    "DASHSCOPE_EMBEDDING_MODEL": "text-embedding-v4",
                    "DASHSCOPE_EMBEDDING_DIM": "2",
                },
                clear=True,
            ):
                config = load_config()
                self.assertFalse(hasattr(config, "dashscope_base_url"))
                self.assertFalse(hasattr(config, "dashscope_api_key"))
                self.assertFalse(hasattr(config, "dashscope_embedding_model"))
                self.assertFalse(hasattr(config, "dashscope_embedding_dim"))


class SearchConfigTests(unittest.TestCase):
    def test_store_path_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.local_db_path, "problems-data/local-index.db")

    def test_search_params_ranges(self) -> None:
        cases = (
            ("ANKLANG_SEARCH_K", "1", "20", "0", "21"),
            ("ANKLANG_MINIMUM_SIMILARITY", "0.0", "1.0", "-0.01", "1.01"),
        )
        for variable, minimum, maximum, below, above in cases:
            for accepted in (minimum, maximum):
                with self.subTest(variable=variable, accepted=accepted), patch.dict(
                    os.environ, {variable: accepted}, clear=True
                ):
                    load_config()
            for rejected in (below, above, "not-a-number"):
                with self.subTest(variable=variable, rejected=rejected), patch.dict(
                    os.environ, {variable: rejected}, clear=True
                ):
                    with self.assertRaises(ConfigError):
                        load_config()

    def test_ingest_interval_minimum_is_60_seconds(self) -> None:
        with patch.dict(
            os.environ, {"ANKLANG_INGEST_INTERVAL_SECONDS": "59"}, clear=True
        ):
            with self.assertRaises(ConfigError):
                load_config()
        with patch.dict(
            os.environ, {"ANKLANG_INGEST_INTERVAL_SECONDS": "60"}, clear=True
        ):
            self.assertEqual(load_config().ingest_interval_seconds, 60)


if __name__ == "__main__":
    unittest.main()
