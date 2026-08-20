from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from anklang.embedding import EmbeddingError
from anklang.ingest import ingest_once
from anklang.sources import (
    RawProblem,
    SourceContractError,
    discover_source_modules,
    validate_raw_problem,
    validate_source_modules,
)
from anklang.sources.example_static import SOURCE_NAME
from anklang.store import ProblemStore
from ui.server import UpstreamSearchBackend


class _Embedder:
    model = "replacement-model"
    dimensions = 2

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.failures: set[str] = set()
        self.calls: list[str] = []

    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        if text in self.failures:
            raise EmbeddingError("synthetic provider failure")
        return self.vectors.get(text, [1.0, 0.0])


class SourceDiscoveryTests(unittest.TestCase):
    def test_example_source_is_discovered(self) -> None:
        modules = discover_source_modules()
        self.assertIn(SOURCE_NAME, {module.SOURCE_NAME for module in modules})
        for module in modules:
            self.assertTrue(callable(module.fetch_new_problems))

    def test_invalid_and_duplicate_source_names_are_rejected(self) -> None:
        fetch = lambda _since: []
        with self.assertRaises(SourceContractError):
            validate_source_modules(
                [SimpleNamespace(SOURCE_NAME="bad/name", fetch_new_problems=fetch)]
            )
        with self.assertRaises(SourceContractError):
            validate_source_modules(
                [
                    SimpleNamespace(SOURCE_NAME="same", fetch_new_problems=fetch),
                    SimpleNamespace(SOURCE_NAME="same", fetch_new_problems=fetch),
                ]
            )


class RawProblemMetadataTests(unittest.TestCase):
    def _problem(self, metadata) -> RawProblem:
        return RawProblem(
            external_id="meta-id",
            title="title",
            statement="statement",
            updated_at="2026-08-14T00:00:00.000Z",
            metadata=metadata,
        )

    def test_str_contains_uppercase_metadata_key_is_rejected(self) -> None:
        with self.assertRaises(SourceContractError):
            validate_raw_problem(self._problem({"BadKey": "value"}))

    def test_metadata_nested_object_is_rejected(self) -> None:
        with self.assertRaises(SourceContractError):
            validate_raw_problem(self._problem({"nested": {"object": "rejected"}}))

    def test_metadata_over_max_keys_is_rejected(self) -> None:
        many = {f"k{i}": i for i in range(17)}
        with self.assertRaises(SourceContractError):
            validate_raw_problem(self._problem(many))

    def test_metadata_oversized_value_is_rejected(self) -> None:
        with self.assertRaises(SourceContractError):
            validate_raw_problem(self._problem({"big": "x" * 513}))

    def test_metadata_whitespace_string_value_is_rejected(self) -> None:
        with self.assertRaises(SourceContractError):
            validate_raw_problem(self._problem({"pad": "  value  "}))
        with self.assertRaises(SourceContractError):
            validate_raw_problem(self._problem({"empty": ""}))

    def test_valid_metadata_canonicalizes_and_round_trips(self) -> None:
        validated = validate_raw_problem(
            self._problem({"zebra": "1", "alpha": 2, "flag": True, "none": None})
        )
        self.assertEqual(
            validated.metadata,
            {"zebra": "1", "alpha": 2, "flag": True, "none": None},
        )


class IncrementalIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.addCleanup(self.store.close)
        self.embedder = _Embedder()

    def test_example_source_is_embedded_and_idempotent(self) -> None:
        first = ingest_once(self.store, self.embedder)  # type: ignore[arg-type]
        second = ingest_once(self.store, self.embedder)  # type: ignore[arg-type]
        self.assertGreater(first.inserted, 0)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.per_source_fetched.get(SOURCE_NAME, 0), 0)
        self.assertTrue(all(problem.embedding for problem in self.store.iter_all()))

    def test_newer_source_version_is_immediately_searchable(self) -> None:
        def fetch_new_problems(since: str | None) -> list[RawProblem]:
            if since is None:
                return [
                    RawProblem(
                        external_id="same-id",
                        title="version one",
                        statement="first statement",
                        updated_at="2026-08-14T00:00:00.000Z",
                    )
                ]
            if since == "2026-08-14T00:00:00.000Z":
                return [
                    RawProblem(
                        external_id="same-id",
                        title="version two",
                        statement="second statement",
                        updated_at="2026-08-14T00:00:01.000Z",
                    )
                ]
            return []

        source = SimpleNamespace(
            SOURCE_NAME="updating-source",
            fetch_new_problems=fetch_new_problems,
        )
        embedder = _Embedder(
            {
                "first statement": [1.0, 0.0],
                "second statement": [0.0, 1.0],
                "first query": [1.0, 0.0],
                "second query": [0.0, 1.0],
            }
        )
        backend = UpstreamSearchBackend(self.store, embedder)  # type: ignore[arg-type]
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            first = ingest_once(self.store, embedder)  # type: ignore[arg-type]
            first_result = backend.search("first query", 5)
            second = ingest_once(self.store, embedder)  # type: ignore[arg-type]
            second_result = backend.search("second query", 5)
            third = ingest_once(self.store, embedder)  # type: ignore[arg-type]

        self.assertEqual(first.inserted, 1)
        self.assertEqual(first_result.candidates[0]["title"], "version one")
        self.assertEqual(second.updated, 1)
        self.assertEqual(second_result.candidates[0]["title"], "version two")
        self.assertEqual(third.fetched, 0)
        self.assertEqual(self.store.count(), 1)

    def test_same_timestamp_conflict_does_not_overwrite_or_advance(self) -> None:
        rounds = [
            RawProblem(
                external_id="same-id",
                title="accepted",
                statement="accepted statement",
                updated_at="2026-08-14T00:00:00.000Z",
            ),
            RawProblem(
                external_id="same-id",
                title="conflict",
                statement="conflicting statement",
                updated_at="2026-08-14T00:00:00.000Z",
            ),
        ]
        calls = 0

        def fetch_new_problems(_since: str | None) -> list[RawProblem]:
            nonlocal calls
            problem = rounds[min(calls, 1)]
            calls += 1
            return [problem]

        source = SimpleNamespace(
            SOURCE_NAME="conflicting-source",
            fetch_new_problems=fetch_new_problems,
        )
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            first = ingest_once(self.store, self.embedder)  # type: ignore[arg-type]
            second = ingest_once(self.store, self.embedder)  # type: ignore[arg-type]

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.skipped, 1)
        stored = self.store.get_problem("conflicting-source", "same-id")
        self.assertEqual(stored.title, "accepted")  # type: ignore[union-attr]
        self.assertEqual(
            self.store.get_cursor("conflicting-source"),
            "2026-08-14T00:00:00.000Z",
        )

    def test_embedding_failure_is_retried_without_advancing_cursor(self) -> None:
        raw = RawProblem(
            external_id="retry-id",
            title="retry",
            statement="retry statement",
            updated_at="2026-08-14T00:00:00.000Z",
        )
        source = SimpleNamespace(
            SOURCE_NAME="retry-source",
            fetch_new_problems=lambda _since: [raw],
        )
        self.embedder.failures.add("retry statement")
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            failed = ingest_once(self.store, self.embedder)  # type: ignore[arg-type]
            self.embedder.failures.clear()
            recovered = ingest_once(self.store, self.embedder)  # type: ignore[arg-type]

        self.assertEqual(failed.embedding_failures, 1)
        self.assertEqual(failed.inserted, 0)
        self.assertEqual(recovered.inserted, 1)
        self.assertEqual(self.store.count(), 1)

    def test_one_source_failure_does_not_block_another(self) -> None:
        failing = SimpleNamespace(
            SOURCE_NAME="failing-source",
            fetch_new_problems=lambda _since: (_ for _ in ()).throw(RuntimeError("synthetic")),
        )
        healthy = SimpleNamespace(
            SOURCE_NAME="healthy-source",
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="healthy-id",
                    title="healthy",
                    statement="healthy statement",
                    updated_at="2026-08-14T00:00:00.000Z",
                )
            ],
        )
        with patch(
            "anklang.ingest.discover_source_modules",
            return_value=[failing, healthy],
        ):
            summary = ingest_once(self.store, self.embedder)  # type: ignore[arg-type]
        self.assertEqual(summary.source_failures, 1)
        self.assertEqual(summary.inserted, 1)

    def test_missing_provider_does_not_create_unsearchable_rows(self) -> None:
        source = SimpleNamespace(
            SOURCE_NAME="no-provider-source",
            fetch_new_problems=lambda _since: [
                RawProblem(
                    external_id="problem-id",
                    title="title",
                    statement="statement",
                    updated_at="2026-08-14T00:00:00.000Z",
                )
            ],
        )
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            summary = ingest_once(self.store, embedder=None)
        self.assertEqual(summary.embedding_failures, 1)
        self.assertEqual(self.store.count(), 0)
        self.assertIsNone(self.store.get_cursor("no-provider-source"))


if __name__ == "__main__":
    unittest.main()
