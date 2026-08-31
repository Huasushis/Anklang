from __future__ import annotations

import time
import threading
import unittest
from unittest.mock import patch

from anklang.provider import ProviderConfig, ProviderRegistry
from anklang.store import EmbeddingIndexSpec, ProblemStore, StoredProblem
from ui.server import UpstreamSearchBackend


class _RebuildEmbedder:
    def __init__(self, model: str, dimensions: int, *, fail: bool = False) -> None:
        self.model = model
        self.dimensions = dimensions
        self.fail = fail
        self.batches: list[list[str]] = []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        if self.fail:
            raise OSError("synthetic failure")
        return [[0.0, 1.0] for _text in texts]


class _BlockingRebuildEmbedder(_RebuildEmbedder):
    def __init__(self, model: str, dimensions: int) -> None:
        super().__init__(model, dimensions)
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.entered.set()
        self.release.wait(3)
        return super().embed_batch(texts)


def _wait_for_rebuild(backend: UpstreamSearchBackend) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = backend.embedding_rebuild_status()
        if status["state"] != "running":
            return status
        time.sleep(0.01)
    raise AssertionError("synthetic rebuild did not finish")


class ProviderRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.old_spec = EmbeddingIndexSpec("old-model", 2)
        self.store.prepare_embedding_writes(
            self.old_spec, base_url="https://old.example.invalid/v1"
        )
        for index in range(2):
            self.store.add_problem(
                StoredProblem(
                    source="synthetic",
                    external_id=f"p-{index}",
                    title=f"problem {index}",
                    url=None,
                    statement=f"statement {index}",
                    embedding=[1.0, 0.0],
                    content_hash=str(index) * 64,
                ),
                index_spec=self.old_spec,
            )
        self.backend = UpstreamSearchBackend(self.store, ProviderRegistry())
        self.addCleanup(self.backend.close)

    def test_changed_provider_rebuilds_every_vector_before_cutover(self) -> None:
        config = ProviderConfig(
            base_url="https://new.example.invalid/v1",
            api_key="synthetic-key",
            model="new-model",
            dimension=2,
        )
        embedder = _RebuildEmbedder(config.model, config.dimension)
        with patch("ui.server.EmbeddingClient", return_value=embedder):
            self.backend.configure_provider(config)
            status = _wait_for_rebuild(self.backend)

        self.assertEqual(status, {"state": "idle", "processed": 2, "total": 2})
        self.assertEqual(embedder.batches, [["statement 0", "statement 1"]])
        self.assertTrue(
            self.store.index_matches_provider(
                EmbeddingIndexSpec(config.model, config.dimension), config.base_url
            )
        )
        self.assertTrue(self.backend.provider.matches(config))
        self.assertTrue(
            all(problem.embedding == [0.0, 1.0] for problem in self.store.iter_all())
        )

        with patch("ui.server.EmbeddingClient") as constructor:
            self.backend.configure_provider(config)
        constructor.assert_not_called()

    def test_failed_rebuild_keeps_old_vectors_and_reports_safe_state(self) -> None:
        old_config = ProviderConfig(
            base_url="https://old.example.invalid/v1",
            api_key="synthetic-old-key",
            model="old-model",
            dimension=2,
        )
        self.backend.provider.configure(
            old_config,
            client=_RebuildEmbedder(old_config.model, old_config.dimension),
        )
        config = ProviderConfig(
            base_url="https://broken.example.invalid/v1",
            api_key="synthetic-key",
            model="new-model",
            dimension=2,
        )
        embedder = _RebuildEmbedder(config.model, config.dimension, fail=True)
        with patch("ui.server.EmbeddingClient", return_value=embedder):
            self.backend.configure_provider(config)
            status = _wait_for_rebuild(self.backend)

        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["reasonCode"], "embedding_rebuild_failed")
        self.assertFalse(self.backend.provider.matches(config))
        self.assertTrue(self.backend.provider.matches(old_config))
        self.assertTrue(
            self.store.index_matches_provider(
                self.old_spec, "https://old.example.invalid/v1"
            )
        )
        self.assertTrue(
            all(problem.embedding == [1.0, 0.0] for problem in self.store.iter_all())
        )

        self.backend.configure_provider(old_config)
        self.assertEqual(
            self.backend.embedding_rebuild_status(),
            {"state": "idle", "processed": 0, "total": 0},
        )

    def test_clear_waits_for_rebuild_batch_and_does_not_install_stale_provider(self) -> None:
        config = ProviderConfig(
            base_url="https://new.example.invalid/v1",
            api_key="synthetic-key",
            model="new-model",
            dimension=2,
        )
        embedder = _BlockingRebuildEmbedder(config.model, config.dimension)
        with patch("ui.server.EmbeddingClient", return_value=embedder):
            self.backend.configure_provider(config)
            self.assertTrue(embedder.entered.wait(2))
            cleared = threading.Event()

            def clear() -> None:
                self.backend.clear_provider()
                cleared.set()

            clearer = threading.Thread(target=clear)
            clearer.start()
            time.sleep(0.1)
            self.assertFalse(cleared.is_set())
            self.assertIsNone(self.backend.provider.acquire())
            embedder.release.set()
            clearer.join(2)

        self.assertTrue(cleared.is_set())
        self.assertEqual(
            self.backend.embedding_rebuild_status(),
            {"state": "idle", "processed": 0, "total": 0},
        )
        self.assertFalse(self.backend.provider.matches(config))
        self.assertTrue(
            self.store.index_matches_provider(
                self.old_spec, "https://old.example.invalid/v1"
            )
        )


if __name__ == "__main__":
    unittest.main()
