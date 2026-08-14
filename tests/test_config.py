"""环境变量读取测试；不联网，也不读取开发机器的真实环境变量。"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from anklang.config import (
    ConfigError,
    _read_bool,
    _read_optional_url,
    _read_url,
    load_config,
)


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
        self.assertEqual(config.backend, "local_engine")

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


class UrlConfigTests(unittest.TestCase):
    def test_http_url_accepts_host_path_and_valid_port(self) -> None:
        raw = "https://example.com/path"
        with patch.dict(os.environ, {"REQUIRED_URL": raw}, clear=True):
            self.assertEqual(_read_url("REQUIRED_URL", ""), raw)

    def test_dashscope_credentials_are_optional(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertIsNone(config.dashscope_base_url)
        self.assertIsNone(config.dashscope_api_key)
        self.assertEqual(config.dashscope_embedding_model, "text-embedding-v4")
        self.assertEqual(config.dashscope_embedding_dim, 1024)

    def test_dashscope_base_url_validated_when_provided(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DASHSCOPE_BASE_URL": "https://dashscope.example.invalid/compatible-mode/v1",
                "DASHSCOPE_API_KEY": "synthetic-embedding-key",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(
            config.dashscope_base_url,
            "https://dashscope.example.invalid/compatible-mode/v1",
        )
        self.assertEqual(config.dashscope_api_key, "synthetic-embedding-key")

    def test_invalid_dashscope_url_rejected_without_echo(self) -> None:
        private_marker = "private-key-marker"
        for raw in (
            "ftp://example.com",
            "javascript:alert(1)",
            "https://exa%mple.invalid",
            "",
        ):
            with self.subTest(raw=raw), patch.dict(
                os.environ,
                {"DASHSCOPE_BASE_URL": raw, "DASHSCOPE_API_KEY": private_marker},
                clear=True,
            ):
                if raw == "":
                    config = load_config()
                    self.assertIsNone(config.dashscope_base_url)
                    continue
                with self.assertRaises(ConfigError) as raised:
                    load_config()
                self.assertNotIn(private_marker, str(raised.exception))


class LocalEngineConfigTests(unittest.TestCase):
    def test_local_engine_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.backend, "local_engine")
        self.assertEqual(config.local_db_path, "problems-data/local-index.db")
        self.assertEqual(config.local_vector_top_k, 20)
        self.assertEqual(config.local_keyword_top_k, 20)

    def test_search_params_ranges(self) -> None:
        cases = (
            ("ANKLANG_SEARCH_K", "1", "20", "0", "21"),
            ("ANKLANG_MINIMUM_SIMILARITY", "0.0", "1.0", "-0.01", "1.01"),
            ("ANKLANG_LOCAL_VECTOR_TOP_K", "1", "200", "0", "201"),
            ("ANKLANG_LOCAL_KEYWORD_TOP_K", "1", "200", "0", "201"),
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
