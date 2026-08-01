"""环境变量读取测试；不联网，也不读取开发机器的真实环境变量。"""
from __future__ import annotations

import inspect
import os
import unittest
from unittest.mock import patch

from anklang.config import (
    ConfigError,
    _LOCAL_EMBEDDING_TIMEOUT_SECONDS,
    _read_bool,
    _read_optional_url,
    _read_url,
    load_config,
)
from anklang.embedding import EmbeddingClient
from anklang.yuantiji import calculate_search_budget_seconds


class BooleanConfigTests(unittest.TestCase):
    def test_boolean_reader_accepts_only_empty_true_and_false(self) -> None:
        for raw, expected in (("", False), ("true", True), ("false", False)):
            with self.subTest(raw=raw), patch.dict(
                os.environ, {"TEST_BOOLEAN": raw}, clear=True
            ):
                self.assertIs(_read_bool("TEST_BOOLEAN"), expected)

        with patch.dict(os.environ, {"TEST_BOOLEAN": ""}, clear=True):
            self.assertTrue(_read_bool("TEST_BOOLEAN", default=True))

        for raw in ("TRUE", "False", "1", "yes", " true ", " "):
            with self.subTest(raw=raw), patch.dict(
                os.environ, {"TEST_BOOLEAN": raw}, clear=True
            ):
                with self.assertRaises(ConfigError):
                    _read_bool("TEST_BOOLEAN")

    def test_every_boolean_setting_uses_strict_reader(self) -> None:
        fields = (
            ("ANKLANG_LLM_REVIEW", "llm_review_enabled"),
            ("ANKLANG_USE_RERANK", "use_rerank"),
            ("ANKLANG_INGEST_ENABLED", "ingest_enabled"),
            ("ANKLANG_SIMILARITY_BLOCK_ENABLED", "similarity_block_enabled"),
            ("ANKLANG_REQUIRE_SERVICE_TOKEN", "require_service_token"),
        )
        for variable, field in fields:
            environment = {variable: "true"}
            if variable == "ANKLANG_LLM_REVIEW":
                environment.update(
                    {
                        "ANKLANG_LLM_BASE_URL": "https://model.example.invalid/v1",
                        "ANKLANG_LLM_API_KEY": "synthetic-key",
                    }
                )
            if variable == "ANKLANG_REQUIRE_SERVICE_TOKEN":
                environment["ANKLANG_SERVICE_TOKEN"] = "synthetic-token-123456"
            with self.subTest(variable=variable), patch.dict(
                os.environ, environment, clear=True
            ):
                self.assertTrue(getattr(load_config(), field))

    def test_boolean_typo_is_rejected_without_echoing_it(self) -> None:
        private_marker = "truthy-private-marker"
        for variable in (
            "ANKLANG_LLM_REVIEW",
            "ANKLANG_USE_RERANK",
            "ANKLANG_INGEST_ENABLED",
            "ANKLANG_SIMILARITY_BLOCK_ENABLED",
            "ANKLANG_REQUIRE_SERVICE_TOKEN",
        ):
            with self.subTest(variable=variable), patch.dict(
                os.environ, {variable: private_marker}, clear=True
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


class UrlConfigTests(unittest.TestCase):
    def test_http_url_accepts_host_path_and_valid_port(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REQUIRED_URL": " https://service.example.invalid:8443/api/v1/ ",
                "OPTIONAL_URL": "http://127.0.0.1:8080/compatible-mode/v1/",
            },
            clear=True,
        ):
            self.assertEqual(
                _read_url("REQUIRED_URL", "https://default.example.invalid"),
                "https://service.example.invalid:8443/api/v1",
            )
            self.assertEqual(
                _read_optional_url("OPTIONAL_URL"),
                "http://127.0.0.1:8080/compatible-mode/v1",
            )

        for port in (1, 65_535):
            raw = f"https://service.example.invalid:{port}/api"
            with self.subTest(port=port), patch.dict(
                os.environ, {"REQUIRED_URL": raw}, clear=True
            ):
                self.assertEqual(_read_url("REQUIRED_URL", ""), raw)

    def test_required_and_optional_urls_reject_unsafe_forms(self) -> None:
        invalid_urls = (
            "https://",
            "https:///api/v1",
            "ftp://service.example.invalid/api",
            "https://service.example.invalid:not-a-port/api",
            "https://service.example.invalid:0/api",
            "https://service.example.invalid:65536/api",
            "https://service.example.invalid:/api",
            "https://exa%mple.invalid/api",
            "http://256.256.256.256/api",
            "https://user:password@service.example.invalid/api",
            "https://user@service.example.invalid/api",
            "https://@service.example.invalid/api",
            "https://service.example.invalid/api?mode=test",
            "https://service.example.invalid/api?",
            "https://service.example.invalid/api#section",
            "https://service.example.invalid/api#",
            "https://service.example.invalid/api path",
            "https://service.example.invalid\\api",
        )
        for raw in invalid_urls:
            for reader_name in ("required", "optional"):
                with self.subTest(raw=raw, reader=reader_name), patch.dict(
                    os.environ, {"TEST_URL": raw}, clear=True
                ):
                    with self.assertRaises(ConfigError):
                        if reader_name == "required":
                            _read_url("TEST_URL", "https://default.example.invalid")
                        else:
                            _read_optional_url("TEST_URL")

    def test_url_error_does_not_echo_account_or_password(self) -> None:
        raw = "https://private-user:private-password@service.example.invalid/api"
        with patch.dict(os.environ, {"TEST_URL": raw}, clear=True):
            with self.assertRaises(ConfigError) as raised:
                _read_url("TEST_URL", "https://default.example.invalid")
        message = str(raised.exception)
        self.assertNotIn("private-user", message)
        self.assertNotIn("private-password", message)

        private_port_marker = "private-port-marker"
        with patch.dict(
            os.environ,
            {
                "TEST_URL": (
                    f"https://service.example.invalid:{private_port_marker}/api"
                )
            },
            clear=True,
        ):
            with self.assertRaises(ConfigError) as invalid_port:
                _read_url("TEST_URL", "https://default.example.invalid")
        self.assertNotIn(private_port_marker, str(invalid_port.exception))
        self.assertIsNone(invalid_port.exception.__cause__)
        self.assertIsNone(invalid_port.exception.__context__)

    def test_load_config_checks_all_base_url_settings(self) -> None:
        environments = (
            {"YUANTIJI_BASE_URL": "https://yuantiji.example.invalid/api?private=1"},
            {"DASHSCOPE_BASE_URL": "https://model.example.invalid/v1#private"},
            {
                "ANKLANG_LLM_REVIEW": "true",
                "ANKLANG_LLM_BASE_URL": "https://model.example.invalid/v1?private=1",
                "ANKLANG_LLM_API_KEY": "synthetic-key",
            },
        )
        for environment in environments:
            with self.subTest(environment=tuple(environment)), patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaises(ConfigError):
                    load_config()


class RequestWaitBudgetTests(unittest.TestCase):
    def test_local_embedding_timeout_matches_budget_value(self) -> None:
        timeout_default = inspect.signature(EmbeddingClient).parameters[
            "timeout_seconds"
        ].default
        self.assertEqual(timeout_default, _LOCAL_EMBEDDING_TIMEOUT_SECONDS)

    def test_default_and_bounded_retry_settings_fit_budget(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            defaults = load_config()
        self.assertEqual(
            calculate_search_budget_seconds(
                defaults.yuantiji_timeout_seconds,
                defaults.yuantiji_minimum_interval_seconds,
                defaults.yuantiji_max_retries,
                defaults.yuantiji_retry_base_delay_seconds,
            ),
            33.0,
        )

        with patch.dict(
            os.environ,
            {
                "YUANTIJI_TIMEOUT_SECONDS": "20",
                "YUANTIJI_MINIMUM_INTERVAL_SECONDS": "0",
                "YUANTIJI_MAX_RETRIES": "3",
                "YUANTIJI_RETRY_BASE_DELAY_SECONDS": "0",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.yuantiji_timeout_seconds, 20.0)
        self.assertEqual(config.yuantiji_max_retries, 3)

    def test_network_wait_cannot_reach_main_system_limit(self) -> None:
        with patch.dict(
            os.environ,
            {
                "YUANTIJI_TIMEOUT_SECONDS": "30",
                "YUANTIJI_MINIMUM_INTERVAL_SECONDS": "0",
                "YUANTIJI_MAX_RETRIES": "3",
                "YUANTIJI_RETRY_BASE_DELAY_SECONDS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(ConfigError) as raised:
                load_config()
        self.assertIn("100 秒", str(raised.exception))
        self.assertIn("120 秒", str(raised.exception))

    def test_retry_wait_and_request_interval_are_included(self) -> None:
        too_long_environments = (
            {
                "YUANTIJI_TIMEOUT_SECONDS": "17",
                "YUANTIJI_MINIMUM_INTERVAL_SECONDS": "0",
                "YUANTIJI_MAX_RETRIES": "3",
                "YUANTIJI_RETRY_BASE_DELAY_SECONDS": "5",
            },
            {
                "YUANTIJI_TIMEOUT_SECONDS": "20",
                "YUANTIJI_MINIMUM_INTERVAL_SECONDS": "6",
                "YUANTIJI_MAX_RETRIES": "3",
                "YUANTIJI_RETRY_BASE_DELAY_SECONDS": "0",
            },
        )
        for environment in too_long_environments:
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaises(ConfigError):
                    load_config()

    def test_enabled_model_review_is_included_in_total_wait(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ANKLANG_LLM_REVIEW": "true",
                "ANKLANG_LLM_BASE_URL": "https://model.example.invalid/v1",
                "ANKLANG_LLM_API_KEY": "synthetic-key",
                "ANKLANG_LLM_REVIEW_TOP_N": "3",
                "ANKLANG_LLM_TIMEOUT_SECONDS": "25",
            },
            clear=True,
        ):
            with self.assertRaises(ConfigError):
                load_config()

    def test_combined_wait_accepts_exact_budget_and_rejects_more(self) -> None:
        common_environment = {
            "ANKLANG_LLM_REVIEW": "true",
            "ANKLANG_LLM_BASE_URL": "https://model.example.invalid/v1",
            "ANKLANG_LLM_API_KEY": "synthetic-key",
            "ANKLANG_LLM_REVIEW_TOP_N": "1",
        }
        with patch.dict(
            os.environ,
            dict(common_environment, ANKLANG_LLM_TIMEOUT_SECONDS="67"),
            clear=True,
        ):
            self.assertTrue(load_config().llm_review_enabled)

        with patch.dict(
            os.environ,
            dict(common_environment, ANKLANG_LLM_TIMEOUT_SECONDS="67.01"),
            clear=True,
        ):
            with self.assertRaises(ConfigError):
                load_config()

    def test_local_backend_without_embedding_does_not_count_upstream_wait(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ANKLANG_BACKEND": "local_engine",
                "ANKLANG_LLM_REVIEW": "true",
                "ANKLANG_LLM_BASE_URL": "https://model.example.invalid/v1",
                "ANKLANG_LLM_API_KEY": "synthetic-key",
                "ANKLANG_LLM_REVIEW_TOP_N": "5",
                "ANKLANG_LLM_TIMEOUT_SECONDS": "20",
                "YUANTIJI_TIMEOUT_SECONDS": "60",
                "YUANTIJI_MINIMUM_INTERVAL_SECONDS": "60",
                "YUANTIJI_MAX_RETRIES": "3",
                "YUANTIJI_RETRY_BASE_DELAY_SECONDS": "5",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.backend, "local_engine")
        self.assertTrue(config.llm_review_enabled)
        self.assertIsNone(config.dashscope_api_key)

    def test_local_embedding_wait_is_included_in_total(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ANKLANG_BACKEND": "local_engine",
                "DASHSCOPE_BASE_URL": "https://model.example.invalid/v1",
                "DASHSCOPE_API_KEY": "synthetic-embedding-key",
                "ANKLANG_LLM_REVIEW": "true",
                "ANKLANG_LLM_BASE_URL": "https://model.example.invalid/v1",
                "ANKLANG_LLM_API_KEY": "synthetic-review-key",
                "ANKLANG_LLM_REVIEW_TOP_N": "4",
                "ANKLANG_LLM_TIMEOUT_SECONDS": "20",
            },
            clear=True,
        ):
            with self.assertRaises(ConfigError):
                load_config()


if __name__ == "__main__":
    unittest.main()
