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

from anklang.store import ProblemStore


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

        first = store.add_problem(
            source="s",
            external_id="1",
            title="t1",
            statement="body1",
            content_hash="h1",
            embedding=[1.0, 0.0],
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
        self.store.add_problem(
            source="s",
            external_id="1",
            title="t",
            statement="body",
            content_hash="h",
            embedding=vector,
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
        problems = self.store.iter_all()
        self.assertIsNone(problems[0].embedding)

        missing = self.store.iter_missing_embeddings()
        self.assertEqual(len(missing), 1)
        self.assertTrue(
            self.store.update_embedding(
                missing[0].id,
                [1.0, 2.0, 3.0],
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


if __name__ == "__main__":
    unittest.main()
