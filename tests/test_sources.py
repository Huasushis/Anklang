"""源插件框架的端到端测试：example_static 源经框架发现、抓取、入库后，store 里能
查到；增量游标推进后不会重复拉取。不做真实网络调用——example_static 本身就是读
本地 JSON 文件的示例源，不依赖任何外部服务。
"""
from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from anklang.ingest import ingest_once, main
from anklang.sources import (
    RawProblem,
    SourceContractError,
    discover_source_modules,
    validate_source_modules,
)
from anklang.sources import example_static as example_static_source
from anklang.sources.example_static import SOURCE_NAME, fetch_new_problems
from anklang.store import ProblemStore


class ExampleStaticSourceTests(unittest.TestCase):
    def test_fetch_without_since_returns_all(self) -> None:
        problems = fetch_new_problems(since=None)
        self.assertGreaterEqual(len(problems), 3)
        external_ids = {p.external_id for p in problems}
        self.assertIn("demo-1001", external_ids)

    def test_fetch_with_since_filters_older_items(self) -> None:
        all_problems = fetch_new_problems(since=None)
        latest = max(p.updated_at for p in all_problems if p.updated_at)
        newer = fetch_new_problems(since=latest)
        self.assertEqual(newer, [])

    def test_fetch_with_since_before_first_item_returns_all(self) -> None:
        problems = fetch_new_problems(since="2000-01-01T00:00:00.000Z")
        self.assertEqual(len(problems), len(fetch_new_problems(since=None)))

    def test_json_field_types_are_not_silently_converted(self) -> None:
        with patch.object(
            example_static_source.json,
            "load",
            return_value=[
                {
                    "external_id": 123,
                    "title": 456,
                    "statement": 789,
                    "url": None,
                    "updated_at": None,
                }
            ],
        ):
            problems = fetch_new_problems(since=None)

        self.assertEqual(problems[0].external_id, 123)
        self.assertEqual(problems[0].title, 456)
        self.assertEqual(problems[0].statement, 789)


class SourceDiscoveryTests(unittest.TestCase):
    def test_example_static_is_discovered(self) -> None:
        modules = discover_source_modules()
        names = {module.SOURCE_NAME for module in modules}
        self.assertIn(SOURCE_NAME, names)

    def test_discovered_modules_expose_the_plugin_contract(self) -> None:
        for module in discover_source_modules():
            self.assertTrue(hasattr(module, "SOURCE_NAME"))
            self.assertTrue(callable(getattr(module, "fetch_new_problems", None)))

    def test_invalid_or_duplicate_source_names_are_rejected(self) -> None:
        fetch = lambda _since: []  # noqa: E731 - 测试用最小入口
        for invalid_name in ("UpperCase", "../escape", "", "a" * 81):
            with self.subTest(invalid_name=invalid_name), self.assertRaises(
                SourceContractError
            ):
                validate_source_modules(
                    [
                        SimpleNamespace(
                            SOURCE_NAME=invalid_name,
                            fetch_new_problems=fetch,
                        )
                    ]
                )

        with self.assertRaises(SourceContractError):
            validate_source_modules(
                [
                    SimpleNamespace(
                        SOURCE_NAME="same",
                        fetch_new_problems=fetch,
                    ),
                    SimpleNamespace(
                        SOURCE_NAME="same",
                        fetch_new_problems=fetch,
                    ),
                ]
            )


class IngestFrameworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.addCleanup(self.store.close)

    def test_ingest_once_loads_example_static_into_store(self) -> None:
        summary = ingest_once(self.store, embedder=None)
        self.assertGreater(summary.inserted, 0)
        self.assertIn(SOURCE_NAME, summary.per_source_fetched)

        problems = self.store.iter_all()
        sources = {p.source for p in problems}
        self.assertIn(SOURCE_NAME, sources)
        # 没有传 embedder，走的是"embedding 服务暂不可用"的降级分支，不应该报错，
        # 也不应该产生向量——留给之后的 backfill 补算。
        self.assertTrue(all(p.embedding is None for p in problems if p.source == SOURCE_NAME))

    def test_cursor_prevents_refetch_of_same_items(self) -> None:
        first = ingest_once(self.store, embedder=None)
        second = ingest_once(self.store, embedder=None)
        self.assertGreater(first.inserted, 0)
        # 游标已经推进到第一轮抓到的最大 updated_at，第二轮不应该再拉到同一批数据。
        self.assertEqual(second.per_source_fetched.get(SOURCE_NAME, 0), 0)
        self.assertEqual(second.inserted, 0)

    def test_ingest_is_idempotent_even_without_cursor_advantage(self) -> None:
        first = ingest_once(self.store, embedder=None)
        # 模拟"游标被重置 / 来源不支持增量"的情况：强制回到最早之前，让框架重新
        # 拉一遍全量，验证即使这样也不会产生重复记录（靠 (source, external_id) 去重）。
        self.store.set_cursor(SOURCE_NAME, "2000-01-01")
        second = ingest_once(self.store, embedder=None)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.unchanged, first.inserted)

    def test_cursor_advances_after_ingest(self) -> None:
        self.assertIsNone(self.store.get_cursor(SOURCE_NAME))
        ingest_once(self.store, embedder=None)
        self.assertIsNotNone(self.store.get_cursor(SOURCE_NAME))

    def test_high_legacy_cursor_is_replaced_by_normalized_time(self) -> None:
        source_name = "legacy-cursor"
        updated_at = "2026-01-02T00:00:00.000Z"
        received_since: list[str | None] = []

        def fetch_new_problems(since: str | None) -> list[RawProblem]:
            received_since.append(since)
            return [
                RawProblem(
                    external_id="one",
                    title="one",
                    statement="content",
                    updated_at=updated_at,
                )
            ]

        source = SimpleNamespace(
            SOURCE_NAME=source_name,
            fetch_new_problems=fetch_new_problems,
        )
        self.store.set_cursor(source_name, "zzzz-private-cursor")
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            summary = ingest_once(self.store, embedder=None)

        self.assertEqual(received_since, [None])
        self.assertEqual(summary.inserted, 1)
        self.assertEqual(self.store.get_cursor(source_name), updated_at)

    def test_legacy_cursor_repair_does_not_overwrite_concurrent_cursor(self) -> None:
        source_name = "legacy-cursor-race"
        returned_at = "2026-01-02T00:00:00.000Z"
        concurrent_at = "2026-01-03T00:00:00.000Z"
        legacy_cursor = "zzzz-private-cursor"

        def fetch_new_problems(since: str | None) -> list[RawProblem]:
            self.assertIsNone(since)
            self.store.set_cursor(source_name, concurrent_at)
            return [
                RawProblem(
                    external_id="one",
                    title="one",
                    statement="content",
                    updated_at=returned_at,
                )
            ]

        source = SimpleNamespace(
            SOURCE_NAME=source_name,
            fetch_new_problems=fetch_new_problems,
        )
        self.store.set_cursor(source_name, legacy_cursor)
        with (
            patch("anklang.ingest.discover_source_modules", return_value=[source]),
            patch.object(
                self.store,
                "set_cursor_if_current",
                wraps=self.store.set_cursor_if_current,
            ) as advance_cursor,
        ):
            ingest_once(self.store, embedder=None)

        advance_cursor.assert_called_once_with(
            source_name,
            expected_value=legacy_cursor,
            next_value=returned_at,
        )
        self.assertEqual(self.store.get_cursor(source_name), concurrent_at)

    def test_later_source_version_updates_existing_problem(self) -> None:
        def fetch_new_problems(since: str | None) -> list[RawProblem]:
            if since is None:
                return [
                    RawProblem(
                        external_id="same-id",
                        title="version 1",
                        statement="first content",
                        updated_at="2026-01-01T00:00:00.000Z",
                    )
                ]
            if since == "2026-01-01T00:00:00.000Z":
                return [
                    RawProblem(
                        external_id="same-id",
                        title="version 2",
                        statement="second content",
                        updated_at="2026-01-02T00:00:00.000Z",
                    )
                ]
            return []

        source = SimpleNamespace(
            SOURCE_NAME="updating-example",
            fetch_new_problems=fetch_new_problems,
        )
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            first = ingest_once(self.store, embedder=None)
            second = ingest_once(self.store, embedder=None)
            third = ingest_once(self.store, embedder=None)

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.updated, 1)
        self.assertEqual(third.fetched, 0)
        problem = self.store.iter_all()[0]
        self.assertEqual(problem.title, "version 2")
        self.assertEqual(problem.statement, "second content")
        self.assertEqual(
            self.store.get_cursor("updating-example"),
            "2026-01-02T00:00:00.000Z",
        )

    def test_newest_version_wins_even_when_old_version_is_last(self) -> None:
        source = SimpleNamespace(
            SOURCE_NAME="reverse-order",
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="same-id",
                    title="new",
                    statement="new content",
                    updated_at="2026-01-03T00:00:00.000Z",
                ),
                RawProblem(
                    external_id="same-id",
                    title="old",
                    statement="old content",
                    updated_at="2026-01-02T00:00:00.000Z",
                ),
            ],
        )
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            summary = ingest_once(self.store, embedder=None)

        problem = self.store.iter_all()[0]
        self.assertEqual(summary.inserted, 1)
        self.assertEqual((problem.title, problem.statement), ("new", "new content"))
        self.assertEqual(
            self.store.get_cursor("reverse-order"),
            "2026-01-03T00:00:00.000Z",
        )

    def test_same_version_conflict_is_skipped_without_advancing_cursor(self) -> None:
        timestamp = "2026-01-03T00:00:00.000Z"
        source = SimpleNamespace(
            SOURCE_NAME="conflicting-source",
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="same-id",
                    title="first",
                    statement="first content",
                    updated_at=timestamp,
                ),
                RawProblem(
                    external_id="same-id",
                    title="second",
                    statement="different content",
                    updated_at=timestamp,
                ),
            ],
        )
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            summary = ingest_once(self.store, embedder=None)

        self.assertEqual(summary.skipped, 1)
        self.assertEqual(self.store.count(), 0)
        self.assertIsNone(self.store.get_cursor("conflicting-source"))

    def test_mixed_batch_with_missing_time_does_not_advance_cursor(self) -> None:
        source_name = "mixed-time-source"
        stored_cursor = "2026-01-01T00:00:00.000Z"
        source = SimpleNamespace(
            SOURCE_NAME=source_name,
            fetch_new_problems=lambda since: [
                RawProblem(
                    external_id="with-time",
                    title="with time",
                    statement="first content",
                    updated_at="2026-01-02T00:00:00.000Z",
                ),
                RawProblem(
                    external_id="without-time",
                    title="without time",
                    statement="second content",
                    updated_at=None,
                ),
            ],
        )
        self.store.set_cursor(source_name, stored_cursor)
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            summary = ingest_once(self.store, embedder=None)

        self.assertEqual(summary.inserted, 2)
        self.assertEqual(
            {problem.external_id for problem in self.store.iter_all()},
            {"with-time", "without-time"},
        )
        self.assertEqual(self.store.get_cursor(source_name), stored_cursor)

    def test_invalid_source_time_is_rejected_before_writing(self) -> None:
        source = SimpleNamespace(
            SOURCE_NAME="bad-time",
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="one",
                    title="one",
                    statement="content",
                    updated_at="2026-1-2",
                )
            ],
        )
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            summary = ingest_once(self.store, embedder=None)
        self.assertEqual(summary.source_failures, 1)
        self.assertEqual(self.store.count(), 0)

    def test_invalid_fields_are_rejected_before_embedding_or_writing(self) -> None:
        valid = {
            "external_id": "one",
            "title": "one",
            "statement": "content",
            "url": "https://example.test/problems/one",
            "updated_at": "2026-01-02T00:00:00.000Z",
        }
        invalid_fields = [
            ("external_id_type", {"external_id": 1}),
            ("external_id_empty", {"external_id": ""}),
            ("external_id_bom", {"external_id": "\ufeff"}),
            ("external_id_long", {"external_id": "😀" * 101}),
            ("title_type", {"title": 1}),
            ("title_empty", {"title": " \t"}),
            ("title_bom", {"title": "\ufeff"}),
            ("title_long", {"title": "😀" * 101}),
            ("statement_type", {"statement": 1}),
            ("statement_empty", {"statement": ""}),
            ("statement_bom", {"statement": "\ufeff"}),
            ("statement_long", {"statement": "😀" * 250_001}),
            ("url_type", {"url": 1}),
            ("updated_at_type", {"updated_at": 1}),
        ]

        for index, (case_name, replacement) in enumerate(invalid_fields):
            with self.subTest(case_name=case_name):
                source_name = f"bad-field-{index}"
                stored_cursor = "2026-01-01T00:00:00.000Z"
                fields = {**valid, **replacement}
                source = SimpleNamespace(
                    SOURCE_NAME=source_name,
                    fetch_new_problems=lambda _since, fields=fields: [
                        RawProblem(**fields)  # type: ignore[arg-type]
                    ],
                )
                embed_one = Mock(return_value=[1.0, 0.0])
                embedder = SimpleNamespace(embed_one=embed_one)
                self.store.set_cursor(source_name, stored_cursor)

                with patch(
                    "anklang.ingest.discover_source_modules",
                    return_value=[source],
                ):
                    summary = ingest_once(
                        self.store,
                        embedder=embedder,  # type: ignore[arg-type]
                    )

                self.assertEqual(summary.source_failures, 1)
                embed_one.assert_not_called()
                self.assertEqual(self.store.count(), 0)
                self.assertEqual(self.store.get_cursor(source_name), stored_cursor)

    def test_unsafe_source_urls_are_rejected(self) -> None:
        unsafe_urls = [
            "",
            "ftp://example.test/problem",
            "https://user:secret@example.test/problem",
            "https://example.test/bad path",
            "https://example.test\\@other.test/problem",
            "https://256.256.256.256/problem",
            "http://example.invalid:0/problem",
            "https://example.test:70000/problem",
            "https://%65xample.test/problem",
            "https://example.test/problem\x7f",
            "https://example.test/" + "a" * 2_049,
        ]

        for index, unsafe_url in enumerate(unsafe_urls):
            with self.subTest(index=index):
                source_name = f"bad-url-{index}"
                stored_cursor = "2026-01-01T00:00:00.000Z"
                source = SimpleNamespace(
                    SOURCE_NAME=source_name,
                    fetch_new_problems=lambda _since, unsafe_url=unsafe_url: [
                        RawProblem(
                            external_id="one",
                            title="one",
                            statement="content",
                            url=unsafe_url,
                            updated_at="2026-01-02T00:00:00.000Z",
                        )
                    ],
                )
                self.store.set_cursor(source_name, stored_cursor)
                with patch(
                    "anklang.ingest.discover_source_modules",
                    return_value=[source],
                ):
                    summary = ingest_once(self.store, embedder=None)

                self.assertEqual(summary.source_failures, 1)
                self.assertEqual(self.store.count(), 0)
                self.assertEqual(self.store.get_cursor(source_name), stored_cursor)

    def test_invalid_source_does_not_stop_later_source(self) -> None:
        bad_source = SimpleNamespace(
            SOURCE_NAME="bad-before-good",
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="bad",
                    title="",
                    statement="content",
                    updated_at="2026-01-02T00:00:00.000Z",
                )
            ],
        )
        good_time = "2026-01-03T00:00:00.000Z"
        good_source = SimpleNamespace(
            SOURCE_NAME="good-after-bad",
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="good",
                    title="good",
                    statement="content",
                    url="https://example.test/problems/good",
                    updated_at=good_time,
                )
            ],
        )

        with patch(
            "anklang.ingest.discover_source_modules",
            return_value=[bad_source, good_source],
        ):
            summary = ingest_once(self.store, embedder=None)

        self.assertEqual(summary.source_failures, 1)
        self.assertEqual(summary.inserted, 1)
        self.assertIsNone(self.store.get_cursor("bad-before-good"))
        self.assertEqual(self.store.get_cursor("good-after-bad"), good_time)
        self.assertEqual(
            {problem.external_id for problem in self.store.iter_all()},
            {"good"},
        )

    def test_fetch_exception_is_counted_without_leaking_cli_details(self) -> None:
        sensitive_marker = "PRIVATE_SOURCE_FAILURE_MARKER"
        bad_source_name = "failed-fetch"
        bad_cursor = "2026-01-01T00:00:00.000Z"

        def fail_fetch(_since: str | None) -> list[RawProblem]:
            raise RuntimeError(
                f"{sensitive_marker} "
                "https://private.example.test/api "
                "/private/source/cache.json response=secret"
            )

        bad_source = SimpleNamespace(
            SOURCE_NAME=bad_source_name,
            fetch_new_problems=fail_fetch,
        )
        good_time = "2026-01-03T00:00:00.000Z"
        good_source = SimpleNamespace(
            SOURCE_NAME="good-after-fetch-failure",
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="good",
                    title="good",
                    statement="good content",
                    updated_at=good_time,
                )
            ],
        )
        self.store.set_cursor(bad_source_name, bad_cursor)
        embed_one = Mock(return_value=[1.0, 0.0])
        embedder = SimpleNamespace(embed_one=embed_one)
        stderr = io.StringIO()
        stdout = io.StringIO()
        config = SimpleNamespace(
            local_db_path=":memory:",
            dashscope_api_key="test-key",
            dashscope_base_url="https://embedding.example.test",
            dashscope_embedding_model="test-model",
            dashscope_embedding_dim=2,
        )

        with (
            patch(
                "anklang.ingest.discover_source_modules",
                return_value=[bad_source, good_source],
            ),
            patch("anklang.ingest.load_config", return_value=config),
            patch("anklang.ingest.ProblemStore", return_value=self.store),
            patch("anklang.ingest.EmbeddingClient", return_value=embedder),
            patch("anklang.ingest.sys.stderr", stderr),
            patch("anklang.ingest.sys.stdout", stdout),
        ):
            exit_code = main()

        output = stderr.getvalue() + stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("来源失败 1 个", output)
        self.assertNotIn(sensitive_marker, output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("private.example.test", output)
        self.assertNotIn("/private/source/cache.json", output)
        self.assertNotIn("response=secret", output)
        self.assertEqual(self.store.get_cursor(bad_source_name), bad_cursor)
        self.assertEqual(
            self.store.get_cursor("good-after-fetch-failure"),
            good_time,
        )
        self.assertEqual(
            {problem.external_id for problem in self.store.iter_all()},
            {"good"},
        )
        embed_one.assert_called_once_with("good content")

    def test_fetch_does_not_swallow_base_exceptions(self) -> None:
        source = SimpleNamespace(
            SOURCE_NAME="interrupting-source",
            fetch_new_problems=Mock(side_effect=KeyboardInterrupt),
        )

        with (
            patch(
                "anklang.ingest.discover_source_modules",
                return_value=[source],
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            ingest_once(self.store, embedder=None)

        self.assertEqual(self.store.count(), 0)
        self.assertIsNone(self.store.get_cursor("interrupting-source"))

    def test_repeated_full_import_does_not_recalculate_embedding(self) -> None:
        class _CountingEmbedder:
            def __init__(self) -> None:
                self.calls = 0

            def embed_one(self, _text: str) -> list[float]:
                self.calls += 1
                return [1.0, 0.0]

        timestamp = "2026-01-01T00:00:00.000Z"
        source = SimpleNamespace(
            SOURCE_NAME="full-example",
            # 故意忽略 since，模拟不支持增量、每次都返回全量的来源。
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="one",
                    title="one",
                    statement="content",
                    updated_at=timestamp,
                )
            ],
        )
        embedder = _CountingEmbedder()
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            first = ingest_once(self.store, embedder=embedder)  # type: ignore[arg-type]
            second = ingest_once(self.store, embedder=embedder)  # type: ignore[arg-type]

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(embedder.calls, 1)


if __name__ == "__main__":
    unittest.main()
