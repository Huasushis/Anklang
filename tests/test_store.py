from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from anklang.store import (
    EmbeddingIndexSpec,
    IndexMetadataError,
    ProblemStore,
    StoredProblem,
)


def _problem(
    *,
    title: str = "version one",
    statement: str = "alpha beta",
    vector: list[float] | None = None,
    updated_at: str | None = "2026-08-14T00:00:00.000Z",
) -> StoredProblem:
    return StoredProblem(
        source="synthetic",
        external_id="problem-1",
        title=title,
        url=None,
        statement=statement,
        embedding=vector,
        content_hash=("a" if statement == "alpha beta" else "b") * 64,
        source_updated_at=updated_at,
    )


class ProblemStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.addCleanup(self.store.close)
        self.spec = EmbeddingIndexSpec("replacement-model", 2)

    def test_vector_rows_form_ready_current_snapshot(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        self.assertEqual(
            self.store.add_problem(_problem(vector=[1.0, 0.0]), index_spec=self.spec),
            "inserted",
        )
        snapshot = self.store.search_snapshot(self.spec)
        self.assertTrue(snapshot.vector_ready)
        self.assertEqual(snapshot.vector_status, "ready")
        self.assertEqual(snapshot.problems[0].embedding, [1.0, 0.0])

    def test_provider_identity_mismatch_fails_closed(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        self.store.add_problem(_problem(vector=[1.0, 0.0]), index_spec=self.spec)
        mismatched = EmbeddingIndexSpec("other-model", 2)
        snapshot = self.store.search_snapshot(mismatched)
        self.assertFalse(snapshot.vector_ready)
        self.assertEqual(snapshot.vector_status, "model_mismatch")
        self.assertIsNone(snapshot.problems[0].embedding)
        with self.assertRaises(IndexMetadataError):
            self.store.prepare_embedding_writes(mismatched)

    def test_dimension_mismatch_is_rejected_before_write(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        with self.assertRaises(ValueError):
            self.store.add_problem(_problem(vector=[1.0]), index_spec=self.spec)
        self.assertEqual(self.store.count(), 0)

    def test_same_version_conflict_is_skipped(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        self.store.add_problem(_problem(vector=[1.0, 0.0]), index_spec=self.spec)
        outcome = self.store.add_problem(
            _problem(
                title="conflicting title",
                statement="different statement",
                vector=[0.0, 1.0],
            ),
            index_spec=self.spec,
        )
        self.assertEqual(outcome, "stale")
        self.assertEqual(self.store.get_problem("synthetic", "problem-1").title, "version one")  # type: ignore[union-attr]

    def test_newer_version_updates_without_rebuild(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        self.store.add_problem(_problem(vector=[1.0, 0.0]), index_spec=self.spec)
        outcome = self.store.add_problem(
            _problem(
                title="version two",
                statement="different statement",
                vector=[0.0, 1.0],
                updated_at="2026-08-14T00:00:01.000Z",
            ),
            index_spec=self.spec,
        )
        self.assertEqual(outcome, "updated")
        problem = self.store.get_problem("synthetic", "problem-1")
        self.assertEqual(problem.title, "version two")  # type: ignore[union-attr]
        self.assertEqual(problem.embedding, [0.0, 1.0])  # type: ignore[union-attr]

    def test_identical_reingest_is_idempotent(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        problem = _problem(vector=[1.0, 0.0])
        self.assertEqual(self.store.add_problem(problem, index_spec=self.spec), "inserted")
        self.assertEqual(self.store.add_problem(problem, index_spec=self.spec), "unchanged")
        self.assertEqual(self.store.count(), 1)

    def test_cursor_compare_and_advance_rejects_stale_worker(self) -> None:
        self.assertTrue(
            self.store.compare_and_advance_cursor(
                "synthetic", None, "2026-08-14T00:00:00.000Z"
            )
        )
        self.assertFalse(
            self.store.compare_and_advance_cursor(
                "synthetic", None, "2026-08-14T00:00:01.000Z"
            )
        )
        self.assertEqual(
            self.store.get_cursor("synthetic"), "2026-08-14T00:00:00.000Z"
        )

    def test_corrupt_blob_is_never_exposed_to_search(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        self.store.add_problem(_problem(vector=[1.0, 0.0]), index_spec=self.spec)
        self.store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE problems SET embedding = ?", (sqlite3.Binary(b"bad"),)
        )
        self.store._conn.commit()  # type: ignore[attr-defined]
        snapshot = self.store.search_snapshot(self.spec)
        self.assertFalse(snapshot.vector_ready)
        self.assertEqual(snapshot.vector_status, "invalid_vectors")
        self.assertIsNone(snapshot.problems[0].embedding)

    def test_file_store_reopens_current_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            first = ProblemStore(str(path))
            first.prepare_embedding_writes(self.spec)
            first.add_problem(_problem(vector=[1.0, 0.0]), index_spec=self.spec)
            first.close()
            second = ProblemStore(str(path))
            self.addCleanup(second.close)
            self.assertTrue(second.search_snapshot(self.spec).vector_ready)

    def test_metadata_round_trip_insert_and_read(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        problem = StoredProblem(
            source="synthetic",
            external_id="problem-meta",
            title="meta",
            url=None,
            statement="statement",
            embedding=[1.0, 0.0],
            content_hash="c" * 64,
            source_updated_at="2026-08-14T00:00:00.000Z",
            metadata={"origin": "bzoj", "contest": "noip"},
        )
        self.assertEqual(self.store.add_problem(problem, index_spec=self.spec), "inserted")
        stored = self.store.get_problem("synthetic", "problem-meta")
        self.assertEqual(stored.metadata, {"origin": "bzoj", "contest": "noip"})

    def test_metadata_only_change_is_updated_and_preserves_content_hash(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        base = _problem(vector=[1.0, 0.0])
        self.assertEqual(self.store.add_problem(base, index_spec=self.spec), "inserted")
        with_metadata = StoredProblem(
            source=base.source,
            external_id=base.external_id,
            title=base.title,
            url=base.url,
            statement=base.statement,
            embedding=None,
            content_hash=base.content_hash,
            source_updated_at="2026-08-14T00:00:01.000Z",
            metadata={"origin": "vjudge"},
        )
        # 发送方没有重新计算 embedding；content_hash 保持不变时应保留旧向量。
        outcome = self.store.add_problem(with_metadata, index_spec=None)
        self.assertEqual(outcome, "updated")
        problem = self.store.get_problem("synthetic", "problem-1")
        self.assertEqual(problem.metadata, {"origin": "vjudge"})
        self.assertEqual(problem.content_hash, base.content_hash)
        self.assertEqual(problem.embedding, [1.0, 0.0])

    def test_metadata_identical_reingest_is_unchanged(self) -> None:
        self.store.prepare_embedding_writes(self.spec)
        problem = StoredProblem(
            source="synthetic",
            external_id="problem-meta-2",
            title="meta",
            url=None,
            statement="statement",
            embedding=[1.0, 0.0],
            content_hash="d" * 64,
            source_updated_at="2026-08-14T00:00:00.000Z",
            metadata={"origin": "bzoj"},
        )
        self.assertEqual(self.store.add_problem(problem, index_spec=self.spec), "inserted")
        same = StoredProblem(
            source="synthetic",
            external_id="problem-meta-2",
            title="meta",
            url=None,
            statement="statement",
            embedding=None,
            content_hash="d" * 64,
            source_updated_at="2026-08-14T00:00:00.000Z",
            metadata={"origin": "bzoj"},
        )
        self.assertEqual(self.store.add_problem(same, index_spec=None), "unchanged")

    def test_legacy_database_column_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(str(path))
            conn.executescript(
                """
                CREATE TABLE problems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    statement TEXT NOT NULL,
                    embedding BLOB,
                    created_at TEXT NOT NULL,
                    UNIQUE(source, external_id)
                );
                INSERT INTO problems(source, external_id, title, statement, created_at)
                VALUES ('legacy', '1', 'old title', 'old statement', '2026-01-01T00:00:00.000Z');
                """
            )
            conn.commit()
            conn.close()

            store = ProblemStore(str(path))
            self.addCleanup(store.close)
            columns = {
                row[1]
                for row in store._conn.execute("PRAGMA table_info(problems)").fetchall()
            }
            self.assertIn("metadata", columns)
            problem = store.get_problem("legacy", "1")
            self.assertIsNotNone(problem)
            self.assertEqual(problem.title, "old title")
            self.assertIsNone(problem.metadata)


if __name__ == "__main__":
    unittest.main()
