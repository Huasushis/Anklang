from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from anklang.embedding import EmbeddingClient
from anklang.store import EmbeddingIndexSpec, ProblemStore, StoredProblem
from ui import server as upstream_server


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class _Opener:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls = 0

    def __call__(self, request: object, timeout: float) -> _Response:
        del timeout
        self.calls += 1
        body = json.loads(request.data.decode())  # type: ignore[attr-defined]
        text = body["input"] if isinstance(body["input"], str) else body["input"][0]
        if text not in self.vectors:
            raise OSError("synthetic provider failure")
        return _Response(
            {
                "model": "replacement-model",
                "data": [{"index": 0, "embedding": self.vectors[text]}],
            }
        )


def _embedder(vectors: dict[str, list[float]]) -> tuple[EmbeddingClient, _Opener]:
    opener = _Opener(vectors)
    return (
        EmbeddingClient(
            base_url="https://provider.invalid/compatible-mode/v1",
            api_key="synthetic-token-value",
            model="replacement-model",
            dimensions=2,
            opener=opener,
        ),
        opener,
    )


class UpstreamEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.addCleanup(self.store.close)
        self.spec = EmbeddingIndexSpec("replacement-model", 2)
        self.store.prepare_embedding_writes(self.spec)
        for external_id, title, vector in (
            ("near", "近似候选", [1.0, 0.0]),
            ("middle", "中等候选", [0.5, 0.5]),
            ("far", "较远候选", [0.0, 1.0]),
        ):
            self.store.add_problem(
                StoredProblem(
                    source="synthetic",
                    external_id=external_id,
                    title=title,
                    url=None,
                    statement=f"statement {external_id}",
                    embedding=vector,
                    content_hash=external_id.ljust(64, "0"),
                    source_updated_at="2026-08-14T00:00:00.000Z",
                ),
                index_spec=self.spec,
            )

    def test_preserved_upstream_flow_ranks_candidates(self) -> None:
        embedder, opener = _embedder({"synthetic query": [1.0, 0.0]})
        with (
            patch("ui.server.cosine_all", wraps=upstream_server.cosine_all) as cosine,
            patch("ui.server.collapse", wraps=upstream_server.collapse) as collapse,
            patch("ui.server.mkrow", wraps=upstream_server.mkrow) as mkrow,
        ):
            rows = upstream_server.search(self.store, embedder, "synthetic query", 2)
        self.assertEqual([row["externalId"] for row in rows], ["near", "middle"])
        self.assertEqual(opener.calls, 1)
        cosine.assert_called_once()
        collapse.assert_called_once()
        self.assertEqual(mkrow.call_count, 2)

    def test_backend_returns_strict_ranked_candidates(self) -> None:
        embedder, _ = _embedder({"synthetic query": [1.0, 0.0]})
        backend = upstream_server.UpstreamSearchBackend(self.store, embedder)
        result = backend.search("synthetic query", 1)
        self.assertEqual(result.status, "complete")
        self.assertEqual(
            set(result.candidates[0]),
            {"source", "externalId", "title", "similarity"},
        )

    def test_provider_failure_is_explicit_unavailable(self) -> None:
        embedder, opener = _embedder({})
        result = upstream_server.UpstreamSearchBackend(self.store, embedder).search(
            "synthetic query", 5
        )
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason_code, "search_backend_unavailable")
        self.assertTrue(result.retryable)
        self.assertEqual(result.candidates, [])
        self.assertEqual(opener.calls, 1)

    def test_missing_provider_is_explicit_unavailable(self) -> None:
        result = upstream_server.UpstreamSearchBackend(self.store, None).search(
            "synthetic query", 5
        )
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.retryable)
        self.assertEqual(result.candidates, [])

    def test_model_mismatch_fails_closed_before_provider_call(self) -> None:
        opener = _Opener({"synthetic query": [1.0, 0.0]})
        embedder = EmbeddingClient(
            base_url="https://provider.invalid/compatible-mode/v1",
            api_key="synthetic-token-value",
            model="different-model",
            dimensions=2,
            opener=opener,
        )
        result = upstream_server.UpstreamSearchBackend(self.store, embedder).search(
            "synthetic query", 5
        )
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason_code, "search_backend_invalid")
        self.assertEqual(opener.calls, 0)


if __name__ == "__main__":
    unittest.main()
