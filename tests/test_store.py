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


if __name__ == "__main__":
    unittest.main()
