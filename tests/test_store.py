"""ProblemStore 的单元测试：增删查、去重、向量序列化往返、增量游标读写。

常规用例使用内存数据库；迁移与多连接用例只写入测试创建的临时目录，不做网络调用。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from anklang.store import (
    INDEX_BUILD_REVISION,
    EmbeddingIndexSpec,
    IndexMetadataError,
    ProblemStore,
    calculate_corpus_revision,
)
from anklang.vectormath import pack_embedding


class ProblemStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.addCleanup(self.store.close)

    def test_add_and_count(self) -> None:
        result = self.store.add_problem(
            source="example_static",
            external_id="demo-1",
            title="题目一",
            statement="题面内容",
            content_hash="h1",
        )
        self.assertEqual(result, "inserted")
        self.assertEqual(self.store.count(), 1)

    def test_same_source_problem_is_updated_then_unchanged(self) -> None:
        clock = {"value": "2026-01-01T00:00:00.000Z"}
        store = ProblemStore(":memory:", now=lambda: clock["value"])
        self.addCleanup(store.close)
        spec = EmbeddingIndexSpec("test-model", 2)
        store.prepare_embedding_writes(spec)

        first = store.add_problem(
            source="s",
            external_id="1",
            title="t1",
            statement="body1",
            content_hash="h1",
            embedding=[1.0, 0.0],
            index_spec=spec,
            source_updated_at="2026-01-01T00:00:00.000Z",
        )
        inserted = store.iter_all()[0]
        clock["value"] = "2026-01-02T00:00:00.000Z"
        second = store.add_problem(
            source="s",
            external_id="1",
            title="t2",
            statement="body2",
            content_hash="h2",
            source_updated_at="2026-01-02T00:00:00.000Z",
        )
        updated = store.iter_all()[0]
        clock["value"] = "2026-01-03T00:00:00.000Z"
        third = store.add_problem(
            source="s",
            external_id="1",
            title="t2",
            statement="body2",
            content_hash="h2",
            source_updated_at="2026-01-02T00:00:00.000Z",
        )
        unchanged = store.iter_all()[0]

        self.assertEqual((first, second, third), ("inserted", "updated", "unchanged"))
        self.assertEqual(store.count(), 1)
        self.assertEqual(updated.id, inserted.id)
        self.assertEqual(updated.created_at, inserted.created_at)
        self.assertEqual(updated.updated_at, "2026-01-02T00:00:00.000Z")
        self.assertEqual(unchanged.updated_at, updated.updated_at)
        self.assertEqual((updated.title, updated.statement, updated.content_hash), ("t2", "body2", "h2"))
        self.assertIsNone(updated.embedding)

    def test_old_or_unversioned_change_cannot_overwrite_newer_problem(self) -> None:
        self.store.add_problem(
            source="s",
            external_id="1",
            title="new",
            statement="new body",
            content_hash="new-hash",
            source_updated_at="2026-01-03T00:00:00.000Z",
        )
        old_result = self.store.add_problem(
            source="s",
            external_id="1",
            title="old",
            statement="old body",
            content_hash="old-hash",
            source_updated_at="2026-01-02T00:00:00.000Z",
        )
        unknown_result = self.store.add_problem(
            source="s",
            external_id="1",
            title="unknown",
            statement="unknown body",
            content_hash="unknown-hash",
        )
        current = self.store.iter_all()[0]

        self.assertEqual((old_result, unknown_result), ("skipped", "skipped"))
        self.assertEqual((current.title, current.statement), ("new", "new body"))

    def test_different_sources_with_same_external_id_do_not_collide(self) -> None:
        self.store.add_problem(source="a", external_id="1", title="t", statement="x", content_hash="h")
        self.store.add_problem(source="b", external_id="1", title="t", statement="x", content_hash="h")
        self.assertEqual(self.store.count(), 2)

    def test_embedding_blob_roundtrip(self) -> None:
        vector = [0.1, -0.2, 0.3, 0.0, 12.5]
        spec = EmbeddingIndexSpec("test-model", len(vector))
        self.store.prepare_embedding_writes(spec)
        self.store.add_problem(
            source="s",
            external_id="1",
            title="t",
            statement="body",
            content_hash="h",
            embedding=vector,
            index_spec=spec,
        )
        problems = self.store.iter_all()
        self.assertEqual(len(problems), 1)
        restored = problems[0].embedding
        self.assertIsNotNone(restored)
        assert restored is not None  # 帮类型检查器确认非空
        self.assertEqual(len(restored), len(vector))
        for expected, actual in zip(vector, restored):
            self.assertAlmostEqual(expected, actual, places=5)

    def test_missing_embedding_stays_none_until_backfilled(self) -> None:
        self.store.add_problem(source="s", external_id="1", title="t", statement="body", content_hash="h")
        spec = EmbeddingIndexSpec("test-model", 3)
        self.store.prepare_embedding_writes(spec)
        problems = self.store.iter_all()
        self.assertIsNone(problems[0].embedding)

        missing = self.store.iter_missing_embeddings()
        self.assertEqual(len(missing), 1)
        self.assertTrue(
            self.store.update_embedding(
                missing[0].id,
                [1.0, 2.0, 3.0],
                index_spec=spec,
                expected_content_hash=missing[0].content_hash,
            )
        )

        self.assertEqual(len(self.store.iter_missing_embeddings()), 0)
        refreshed = self.store.iter_all()[0]
        self.assertIsNotNone(refreshed.embedding)
        self.assertFalse(
            self.store.update_embedding(
                missing[0].id,
                [9.0, 9.0, 9.0],
                index_spec=spec,
                expected_content_hash=missing[0].content_hash,
            )
        )
        self.assertEqual(self.store.iter_all()[0].embedding, [1.0, 2.0, 3.0])

    def test_cursor_get_and_set(self) -> None:
        self.assertIsNone(self.store.get_cursor("example_static"))
        self.store.set_cursor("example_static", "2026-01-01")
        self.assertEqual(self.store.get_cursor("example_static"), "2026-01-01")
        self.store.set_cursor("example_static", "2026-02-01")
        self.assertEqual(self.store.get_cursor("example_static"), "2026-02-01")

    def test_cursors_are_isolated_per_source(self) -> None:
        self.store.set_cursor("source-a", "2026-01-01")
        self.store.set_cursor("source-b", "2026-06-01")
        self.assertEqual(self.store.get_cursor("source-a"), "2026-01-01")
        self.assertEqual(self.store.get_cursor("source-b"), "2026-06-01")

    def test_old_backfill_cannot_overwrite_updated_problem(self) -> None:
        self.store.add_problem(
            source="s",
            external_id="1",
            title="t",
            statement="body1",
            content_hash="h1",
            source_updated_at="2026-01-01T00:00:00.000Z",
        )
        old = self.store.iter_all()[0]
        spec = EmbeddingIndexSpec("test-model", 2)
        self.store.prepare_embedding_writes(spec)
        self.store.add_problem(
            source="s",
            external_id="1",
            title="t",
            statement="body2",
            content_hash="h2",
            source_updated_at="2026-01-02T00:00:00.000Z",
        )
        self.assertFalse(
            self.store.update_embedding(
                old.id,
                [1.0, 2.0],
                index_spec=spec,
                expected_content_hash=old.content_hash,
            )
        )
        self.assertIsNone(self.store.iter_all()[0].embedding)

    def test_batch_counts_new_and_updated_rows(self) -> None:
        count = self.store.add_problems_batch(
            [
                {
                    "source": "s",
                    "external_id": "1",
                    "title": "t1",
                    "statement": "a",
                    "content_hash": "h1",
                    "source_updated_at": "2026-01-01T00:00:00.000Z",
                },
                {
                    "source": "s",
                    "external_id": "2",
                    "title": "t2",
                    "statement": "b",
                    "content_hash": "h2",
                    "source_updated_at": "2026-01-01T00:00:00.000Z",
                },
                {
                    "source": "s",
                    "external_id": "1",
                    "title": "t1-dup",
                    "statement": "a2",
                    "content_hash": "h1b",
                    "source_updated_at": "2026-01-02T00:00:00.000Z",
                },
            ]
        )
        self.assertEqual(count, 3)
        self.assertEqual(self.store.count(), 2)
        first = next(problem for problem in self.store.iter_all() if problem.external_id == "1")
        self.assertEqual(first.title, "t1-dup")

    def test_existing_database_gets_updated_at_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE problems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    statement TEXT NOT NULL,
                    embedding BLOB,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source, external_id)
                );
                INSERT INTO problems
                    (source, external_id, title, statement, content_hash, created_at)
                VALUES
                    ('s', '1', 't', 'body', 'h', '2026-01-01T00:00:00.000Z');
                """
            )
            connection.commit()
            connection.close()

            migrated = ProblemStore(str(path))
            try:
                problem = migrated.iter_all()[0]
                self.assertEqual(problem.updated_at, problem.created_at)
                self.assertIsNone(problem.source_updated_at)
            finally:
                migrated.close()

    def test_interrupted_updated_at_migration_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE problems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    statement TEXT NOT NULL,
                    embedding BLOB,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    UNIQUE(source, external_id)
                );
                INSERT INTO problems
                    (source, external_id, title, statement, content_hash,
                     created_at, updated_at)
                VALUES
                    ('s', '1', 't', 'body', 'h',
                     '2026-01-01T00:00:00.000Z', NULL);
                """
            )
            connection.commit()
            connection.close()

            migrated = ProblemStore(str(path))
            try:
                problem = migrated.iter_all()[0]
                self.assertEqual(problem.updated_at, problem.created_at)
            finally:
                migrated.close()

    def test_two_connections_can_migrate_the_same_legacy_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-shared.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE problems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    statement TEXT NOT NULL,
                    embedding BLOB,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source, external_id)
                );
                INSERT INTO problems
                    (source, external_id, title, statement, content_hash, created_at)
                VALUES
                    ('s', '1', 't', 'body', 'h',
                     '2026-01-01T00:00:00.000Z');
                """
            )
            connection.commit()
            connection.close()

            real_connect = sqlite3.connect
            start_barrier = threading.Barrier(3)
            legacy_read_barrier = threading.Barrier(2)

            class CoordinatedCursor:
                def __init__(self, cursor: Any) -> None:
                    self._cursor = cursor

                def fetchall(self) -> Any:
                    rows = self._cursor.fetchall()
                    legacy_read_barrier.wait(timeout=2.0)
                    return rows

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._cursor, name)

            class CoordinatedConnection:
                def __init__(self, wrapped: Any) -> None:
                    self._wrapped = wrapped
                    self._migration_write_lock = False

                @property
                def row_factory(self) -> Any:
                    return self._wrapped.row_factory

                @row_factory.setter
                def row_factory(self, value: Any) -> None:
                    self._wrapped.row_factory = value

                def execute(self, sql: str, *args: Any) -> Any:
                    cursor = self._wrapped.execute(sql, *args)
                    normalized = " ".join(sql.upper().split())
                    if normalized == "BEGIN IMMEDIATE":
                        self._migration_write_lock = True
                    if (
                        normalized == "PRAGMA TABLE_INFO(PROBLEMS)"
                        and not self._migration_write_lock
                    ):
                        # 旧实现没有先取得写锁。让两个连接都读到缺列状态后
                        # 再继续，稳定复现两个连接添加同名列的竞争。
                        return CoordinatedCursor(cursor)
                    return cursor

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._wrapped, name)

            def coordinated_connect(*args: Any, **kwargs: Any) -> Any:
                return CoordinatedConnection(real_connect(*args, **kwargs))

            stores: list[ProblemStore] = []
            errors: list[BaseException] = []

            def open_store() -> None:
                start_barrier.wait()
                try:
                    stores.append(ProblemStore(str(path)))
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=open_store) for _ in range(2)]
            try:
                with patch(
                    "anklang.store.sqlite3.connect",
                    side_effect=coordinated_connect,
                ):
                    for thread in threads:
                        thread.start()
                    start_barrier.wait(timeout=2.0)
                    for thread in threads:
                        thread.join(timeout=3.0)

                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(len(stores), 2)
            finally:
                for store in stores:
                    store.close()

            verification = real_connect(path)
            try:
                columns = {
                    str(row[1])
                    for row in verification.execute(
                        "PRAGMA table_info(problems)"
                    ).fetchall()
                }
                row = verification.execute(
                    "SELECT source, external_id, title, statement, content_hash, "
                    "created_at, updated_at, source_updated_at FROM problems"
                ).fetchone()
            finally:
                verification.close()

            self.assertIn("updated_at", columns)
            self.assertIn("source_updated_at", columns)
            self.assertEqual(
                row,
                (
                    "s",
                    "1",
                    "t",
                    "body",
                    "h",
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                    None,
                ),
            )

    def test_two_connections_do_not_race_when_inserting_same_problem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared.db"
            store = ProblemStore(str(path))
            self.addCleanup(store.close)
            other = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
            other.execute("BEGIN IMMEDIATE")
            timestamp = "2026-01-01T00:00:00.000Z"
            other.execute(
                """
                INSERT INTO problems
                    (source, external_id, title, statement, content_hash,
                     source_updated_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("s", "1", "t", "body", "h", timestamp, timestamp, timestamp),
            )

            results: list[str] = []
            errors: list[BaseException] = []

            def add_same_problem() -> None:
                try:
                    results.append(
                        store.add_problem(
                            source="s",
                            external_id="1",
                            title="t",
                            statement="body",
                            content_hash="h",
                            source_updated_at=timestamp,
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=add_same_problem)
            thread.start()
            thread.join(timeout=0.05)
            self.assertTrue(thread.is_alive())
            other.commit()
            other.close()
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, ["unchanged"])
            self.assertEqual(store.count(), 1)

    def test_cursor_update_requires_the_value_seen_at_start(self) -> None:
        old = "2026-01-01T00:00:00.000Z"
        middle = "2026-01-02T00:00:00.000Z"
        newest = "2026-01-03T00:00:00.000Z"
        self.store.set_cursor("s", old)

        self.assertTrue(self.store.set_cursor_if_current("s", old, newest))
        self.assertFalse(self.store.set_cursor_if_current("s", old, middle))
        self.assertEqual(self.store.get_cursor("s"), newest)

    def test_legacy_cursor_can_be_replaced_by_normalized_time(self) -> None:
        normalized = "2026-01-01T00:00:00.000Z"
        self.store.set_cursor("s", "zzzz-private-cursor")

        self.assertTrue(
            self.store.set_cursor_if_current(
                "s",
                expected_value="zzzz-private-cursor",
                next_value=normalized,
            )
        )
        self.assertEqual(self.store.get_cursor("s"), normalized)


class IndexMetadataTests(unittest.TestCase):
    def test_incremental_writes_do_not_repeat_full_index_scans(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        store.prepare_keyword_writes()
        identities: list[tuple[str, str, str]] = []
        with (
            patch.object(
                store,
                "_actual_index_state_locked",
                side_effect=AssertionError("unexpected full index scan"),
            ),
            patch.object(
                store,
                "_problem_rows_locked",
                side_effect=AssertionError("unexpected full problem scan"),
            ),
        ):
            for index in range(250):
                external_id = f"problem-{index}"
                content_hash = f"hash-{index}"
                identities.append(("scale-test", external_id, content_hash))
                store.add_problem(
                    source="scale-test",
                    external_id=external_id,
                    title="synthetic",
                    statement="synthetic statement",
                    content_hash=content_hash,
                )
        metadata = store.get_index_metadata()
        assert metadata is not None
        self.assertEqual(metadata.problem_count, 250)
        self.assertEqual(metadata.embedding_rows, 0)
        self.assertEqual(
            metadata.corpus_revision,
            calculate_corpus_revision(identities),
        )

    def test_preflight_revision_query_never_reads_statements_or_vector_blobs(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        for index in range(3):
            store.add_problem(
                source="s",
                external_id=str(index),
                title="synthetic title",
                statement="synthetic statement",
                content_hash=f"hash-{index}",
            )
        traced: list[str] = []
        store._conn.set_trace_callback(traced.append)  # noqa: SLF001
        store.prepare_embedding_writes(EmbeddingIndexSpec("model-a", 2))
        store._conn.set_trace_callback(None)  # noqa: SLF001
        problem_selects = [
            " ".join(statement.lower().split())
            for statement in traced
            if " from problems" in statement.lower()
        ]
        self.assertIn(
            "select source, external_id, content_hash from problems",
            problem_selects,
        )
        revision_query = next(
            statement
            for statement in problem_selects
            if "source, external_id, content_hash" in statement
        )
        self.assertNotIn("embedding", revision_query)
        self.assertTrue(
            all("statement" not in statement for statement in problem_selects)
        )
        self.assertTrue(
            all("title" not in statement for statement in problem_selects)
        )

    def test_health_uses_verified_metadata_and_external_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.db"
            spec = EmbeddingIndexSpec("model-a", 2)
            store = ProblemStore(str(path))
            try:
                store.prepare_embedding_writes(spec)
                store.add_problem(
                    source="s",
                    external_id="one",
                    title="one",
                    statement="large synthetic statement",
                    content_hash="h",
                    embedding=[1.0, 0.0],
                    index_spec=spec,
                )
                traced: list[str] = []
                store._conn.set_trace_callback(traced.append)  # noqa: SLF001
                inspection = store.inspect_index(spec)
                store._conn.set_trace_callback(None)  # noqa: SLF001
                self.assertTrue(inspection.vector_ready)
                self.assertIsNotNone(inspection.cache_identity)
                health_sql = " ".join(traced).lower()
                self.assertNotIn(" from problems", health_sql)
                self.assertNotIn("statement", health_sql)
                self.assertNotIn("select embedding", health_sql)

                external = sqlite3.connect(path)
                external.execute(
                    "UPDATE problems SET title = 'changed externally' WHERE id = 1"
                )
                external.commit()
                external.close()

                stale = store.inspect_index(spec)
                self.assertFalse(stale.metadata_ready)
                self.assertFalse(stale.vector_ready)
                self.assertEqual(stale.status, "verification_required")
                self.assertIsNone(stale.problem_count)

                refreshed = store.search_snapshot(spec)
                self.assertTrue(refreshed.vector_ready)
                self.assertTrue(store.inspect_index(spec).vector_ready)
            finally:
                store.close()

    def test_commit_to_record_window_keeps_the_observed_data_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record-window.db"
            spec = EmbeddingIndexSpec("model-a", 2)
            store = ProblemStore(str(path))
            try:
                store.prepare_embedding_writes(spec)
                store.add_problem(
                    source="s",
                    external_id="one",
                    title="one",
                    statement="body",
                    content_hash="h",
                    embedding=[1.0, 0.0],
                    index_spec=spec,
                )
                original_record = store._record_verification_locked  # noqa: SLF001
                mutation_done = False

                def mutate_before_record(*args: Any, **kwargs: Any) -> None:
                    nonlocal mutation_done
                    if not mutation_done:
                        mutation_done = True
                        external = sqlite3.connect(path)
                        external.execute(
                            "UPDATE problems SET title = 'commit-window-change' "
                            "WHERE id = 1"
                        )
                        external.commit()
                        external.close()
                    original_record(*args, **kwargs)

                with patch.object(
                    store,
                    "_record_verification_locked",
                    side_effect=mutate_before_record,
                ):
                    snapshot = store.search_snapshot(spec)
                self.assertTrue(snapshot.vector_ready)
                inspection = store.inspect_index(spec)
                self.assertEqual(inspection.status, "verification_required")
                self.assertFalse(inspection.vector_ready)
            finally:
                store.close()

    def test_metadata_is_machine_generated_and_tracks_actual_rows(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        spec = EmbeddingIndexSpec("model-a", 2)
        initial = store.prepare_embedding_writes(spec)
        self.assertEqual(initial.problem_count, 0)
        self.assertEqual(initial.embedding_rows, 0)
        self.assertEqual(initial.index_build_revision, INDEX_BUILD_REVISION)

        store.add_problem(
            source="source-a",
            external_id="one",
            title="one",
            statement="first",
            content_hash="hash-one",
            embedding=[1.0, 0.0],
            index_spec=spec,
            source_updated_at="2026-01-01T00:00:00.000Z",
        )
        inserted = store.get_index_metadata()
        assert inserted is not None
        self.assertEqual((inserted.problem_count, inserted.embedding_rows), (1, 1))
        self.assertEqual(
            inserted.corpus_revision,
            calculate_corpus_revision([("source-a", "one", "hash-one")]),
        )

        store.add_problem(
            source="source-a",
            external_id="one",
            title="one",
            statement="second",
            content_hash="hash-two",
            source_updated_at="2026-01-02T00:00:00.000Z",
        )
        cleared = store.get_index_metadata()
        assert cleared is not None
        self.assertEqual((cleared.problem_count, cleared.embedding_rows), (1, 0))
        self.assertEqual(
            cleared.corpus_revision,
            calculate_corpus_revision([("source-a", "one", "hash-two")]),
        )

    def test_keyword_metadata_can_upgrade_only_while_no_vectors_exist(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        keyword_metadata = store.prepare_keyword_writes()
        self.assertEqual(keyword_metadata.embedding_dimensions, 0)
        store.add_problem(
            source="s",
            external_id="one",
            title="one",
            statement="body",
            content_hash="h",
        )
        spec = EmbeddingIndexSpec("model-a", 2)
        upgraded = store.prepare_embedding_writes(spec)
        self.assertEqual(upgraded.embedding_model, "model-a")
        self.assertEqual(upgraded.embedding_dimensions, 2)
        problem = store.iter_missing_embeddings()[0]
        self.assertTrue(
            store.update_embedding(
                problem.id,
                [1.0, 0.0],
                index_spec=spec,
                expected_content_hash="h",
            )
        )

    def test_legacy_vectors_are_preserved_and_cannot_be_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-vector.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE problems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    statement TEXT NOT NULL,
                    embedding BLOB,
                    content_hash TEXT NOT NULL,
                    source_updated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source, external_id)
                );
                """
            )
            blob = pack_embedding([1.0, 0.0])
            connection.execute(
                "INSERT INTO problems VALUES "
                "(1, 's', 'one', 'one', NULL, 'keyword body', ?, 'h', NULL, ?, ?)",
                (blob, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
            )
            connection.commit()
            connection.close()

            store = ProblemStore(str(path))
            try:
                spec = EmbeddingIndexSpec("model-a", 2)
                with self.assertRaises(IndexMetadataError) as caught:
                    store.prepare_embedding_writes(spec)
                self.assertEqual(caught.exception.status, "legacy_vectors")
                snapshot = store.search_snapshot(spec)
                self.assertFalse(snapshot.vector_ready)
                self.assertEqual(snapshot.vector_status, "legacy_vectors")
                self.assertIsNone(snapshot.problems[0].embedding)
            finally:
                store.close()
            verification = sqlite3.connect(path)
            try:
                self.assertEqual(
                    verification.execute(
                        "SELECT embedding FROM problems WHERE id = 1"
                    ).fetchone()[0],
                    blob,
                )
                self.assertEqual(
                    verification.execute(
                        "SELECT COUNT(*) FROM index_metadata"
                    ).fetchone()[0],
                    0,
                )
            finally:
                verification.close()

    def test_same_dimension_other_model_and_dimension_change_are_rejected(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        store.prepare_embedding_writes(EmbeddingIndexSpec("model-a", 2))
        for spec, status in (
            (EmbeddingIndexSpec("model-b", 2), "model_mismatch"),
            (EmbeddingIndexSpec("model-a", 3), "dimension_mismatch"),
        ):
            with self.subTest(status=status):
                with self.assertRaises(IndexMetadataError) as caught:
                    store.prepare_embedding_writes(spec)
                self.assertEqual(caught.exception.status, status)

    def test_build_revision_change_is_rejected_without_rewriting_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build-change.db"
            store = ProblemStore(str(path))
            spec = EmbeddingIndexSpec("model-a", 2)
            store.prepare_embedding_writes(spec)
            store.add_problem(
                source="s",
                external_id="one",
                title="one",
                statement="body",
                content_hash="h",
                embedding=[1.0, 0.0],
                index_spec=spec,
            )
            store.close()
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE index_metadata SET value = 'future-builder' "
                "WHERE key = 'index_build_revision'"
            )
            connection.commit()
            before = connection.execute(
                "SELECT embedding FROM problems WHERE external_id = 'one'"
            ).fetchone()[0]
            connection.close()

            reopened = ProblemStore(str(path))
            try:
                with self.assertRaises(IndexMetadataError) as caught:
                    reopened.prepare_embedding_writes(spec)
                self.assertEqual(caught.exception.status, "build_mismatch")
            finally:
                reopened.close()
            verification = sqlite3.connect(path)
            try:
                self.assertEqual(
                    verification.execute(
                        "SELECT embedding FROM problems WHERE external_id = 'one'"
                    ).fetchone()[0],
                    before,
                )
            finally:
                verification.close()

    def test_stale_counts_and_extra_metadata_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale-metadata.db"
            spec = EmbeddingIndexSpec("model-a", 2)
            store = ProblemStore(str(path))
            store.prepare_embedding_writes(spec)
            store.close()
            connection = sqlite3.connect(path)
            timestamp = "2026-01-01T00:00:00.000Z"
            connection.execute(
                "INSERT INTO problems "
                "(source, external_id, title, statement, content_hash, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("s", "one", "one", "body", "h", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO index_metadata (key, value) VALUES ('future_key', '1')"
            )
            connection.commit()
            connection.close()

            reopened = ProblemStore(str(path))
            try:
                snapshot = reopened.search_snapshot(spec)
                self.assertFalse(snapshot.vector_ready)
                self.assertEqual(snapshot.vector_status, "metadata_shape")
                self.assertEqual(len(snapshot.problems), 1)
                with self.assertRaises(IndexMetadataError):
                    reopened.prepare_embedding_writes(spec)
            finally:
                reopened.close()
            connection = sqlite3.connect(path)
            connection.execute(
                "DELETE FROM index_metadata WHERE key = 'future_key'"
            )
            connection.commit()
            connection.close()
            stale = ProblemStore(str(path))
            try:
                snapshot = stale.search_snapshot(spec)
                self.assertFalse(snapshot.vector_ready)
                self.assertEqual(snapshot.vector_status, "metadata_stale")
                with self.assertRaises(IndexMetadataError) as caught:
                    stale.prepare_embedding_writes(spec)
                self.assertEqual(caught.exception.status, "metadata_stale")
            finally:
                stale.close()

    def test_one_corrupt_blob_hides_all_vectors_from_the_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt-vector.db"
            spec = EmbeddingIndexSpec("model-a", 2)
            store = ProblemStore(str(path))
            store.prepare_embedding_writes(spec)
            for external_id, vector in (
                ("good", [1.0, 0.0]),
                ("bad", [0.0, 1.0]),
            ):
                store.add_problem(
                    source="s",
                    external_id=external_id,
                    title=external_id,
                    statement=f"{external_id} keyword body",
                    content_hash=f"hash-{external_id}",
                    embedding=vector,
                    index_spec=spec,
                )
            store.close()
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE problems SET embedding = ? WHERE external_id = 'bad'",
                (pack_embedding([1.0]),),
            )
            connection.commit()
            connection.close()

            reopened = ProblemStore(str(path))
            try:
                snapshot = reopened.search_snapshot(spec)
                self.assertFalse(snapshot.vector_ready)
                self.assertEqual(snapshot.vector_status, "invalid_vectors")
                self.assertTrue(
                    all(problem.embedding is None for problem in snapshot.problems)
                )
            finally:
                reopened.close()

    def test_missing_vector_makes_the_whole_vector_snapshot_incomplete(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        spec = EmbeddingIndexSpec("model-a", 2)
        store.prepare_embedding_writes(spec)
        store.add_problem(
            source="s",
            external_id="ready",
            title="ready",
            statement="ready keyword",
            content_hash="ready-hash",
            embedding=[1.0, 0.0],
            index_spec=spec,
        )
        store.add_problem(
            source="s",
            external_id="missing",
            title="missing",
            statement="missing keyword",
            content_hash="missing-hash",
        )

        snapshot = store.search_snapshot(spec)

        self.assertFalse(snapshot.vector_ready)
        self.assertEqual(snapshot.vector_status, "incomplete_vectors")
        self.assertTrue(all(problem.embedding is None for problem in snapshot.problems))

        keyword_snapshot = store.search_snapshot(None)
        self.assertFalse(keyword_snapshot.vector_ready)
        self.assertEqual(keyword_snapshot.vector_status, "incomplete_vectors")
        self.assertIsNone(keyword_snapshot.cache_identity)
        self.assertEqual(store.inspect_index(None).status, "incomplete_vectors")

    def test_concurrent_initialization_allows_only_one_conflicting_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent-init.db"
            first = ProblemStore(str(path))
            second = ProblemStore(str(path))
            barrier = threading.Barrier(3)
            successes: list[str] = []
            failures: list[str] = []

            def prepare(store: ProblemStore, model: str) -> None:
                barrier.wait()
                try:
                    store.prepare_embedding_writes(EmbeddingIndexSpec(model, 2))
                    successes.append(model)
                except IndexMetadataError as error:
                    failures.append(error.status)

            threads = [
                threading.Thread(target=prepare, args=(first, "model-a")),
                threading.Thread(target=prepare, args=(second, "model-b")),
            ]
            try:
                for thread in threads:
                    thread.start()
                barrier.wait(timeout=2.0)
                for thread in threads:
                    thread.join(timeout=3.0)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(len(successes), 1)
                self.assertEqual(failures, ["model_mismatch"])
            finally:
                first.close()
                second.close()

    def test_concurrent_backfill_updates_metadata_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent-backfill.db"
            first = ProblemStore(str(path))
            second = ProblemStore(str(path))
            spec = EmbeddingIndexSpec("model-a", 2)
            first.prepare_embedding_writes(spec)
            first.add_problem(
                source="s",
                external_id="one",
                title="one",
                statement="body",
                content_hash="h",
            )
            second.prepare_embedding_writes(spec)
            problem_id = first.iter_missing_embeddings()[0].id
            barrier = threading.Barrier(3)
            results: list[bool] = []
            failures: list[str] = []

            def update(store: ProblemStore) -> None:
                barrier.wait()
                try:
                    results.append(
                        store.update_embedding(
                            problem_id,
                            [1.0, 0.0],
                            index_spec=spec,
                            expected_content_hash="h",
                        )
                    )
                except IndexMetadataError as error:
                    failures.append(error.status)

            threads = [
                threading.Thread(target=update, args=(first,)),
                threading.Thread(target=update, args=(second,)),
            ]
            try:
                for thread in threads:
                    thread.start()
                barrier.wait(timeout=2.0)
                for thread in threads:
                    thread.join(timeout=3.0)
                self.assertEqual(results, [True])
                self.assertEqual(failures, ["verification_required"])
                metadata = first.get_index_metadata()
                assert metadata is not None
                self.assertEqual(metadata.embedding_rows, 1)
            finally:
                first.close()
                second.close()

    def test_metadata_failure_rolls_back_the_problem_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollback.db"
            initial = ProblemStore(str(path))
            initial.prepare_embedding_writes(EmbeddingIndexSpec("model-a", 2))
            initial.close()
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TRIGGER reject_problem_count_update
                BEFORE UPDATE OF value ON index_metadata
                WHEN NEW.key = 'problem_count'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic metadata failure');
                END;
                """
            )
            connection.commit()
            connection.close()

            store = ProblemStore(str(path))
            try:
                store.prepare_embedding_writes(
                    EmbeddingIndexSpec("model-a", 2)
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.add_problem(
                        source="s",
                        external_id="one",
                        title="one",
                        statement="body",
                        content_hash="h",
                    )
                self.assertEqual(store.count(), 0)
                metadata = store.get_index_metadata()
                assert metadata is not None
                self.assertEqual(metadata.problem_count, 0)
                self.assertEqual(metadata.embedding_rows, 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
