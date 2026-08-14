from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any

from anklang.config import AppConfig
from anklang.embedding import EmbeddingClient
from anklang.http_api import AnklangService, make_handler
from anklang.store import EmbeddingIndexSpec, ProblemStore, StoredProblem
from ui.server import UpstreamSearchBackend, build_backend


def _config(**overrides: Any) -> AppConfig:
    values: dict[str, Any] = {
        "port": 8730,
        "service_token": None,
        "search_k": 5,
        "minimum_similarity": 0.1,
        "local_db_path": ":memory:",
    }
    values.update(overrides)
    return AppConfig(**values)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.body


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
            raise OSError("synthetic failure")
        return _Response(
            {
                "model": "replacement-model",
                "data": [{"index": 0, "embedding": self.vectors[text]}],
            }
        )


class _Harness:
    def __init__(self, service: AnklangService) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, Any]]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        status = response.status
        connection.close()
        return status, result


def _request() -> dict[str, object]:
    return {
        "apiVersion": "2",
        "requestId": "12345678-1234-4234-8234-123456789abc",
        "contentHash": "a" * 64,
        "problem": {
            "title": "synthetic title",
            "type": "traditional",
            "tagIds": ["synthetic"],
            "basicStatement": "synthetic query",
        },
    }


class BackendAssemblyTests(unittest.TestCase):
    def test_build_backend_uses_preserved_upstream_entrypoint(self) -> None:
        backend = build_backend(_config())
        self.addCleanup(backend.close)
        self.assertIsInstance(backend, UpstreamSearchBackend)
        self.assertIsNone(backend.embedder)
        self.assertEqual(backend.describe_health()["backend"], "upstream-v2")

    def test_configured_replacement_provider_is_built(self) -> None:
        backend = build_backend(
            _config(
                dashscope_base_url="https://provider.invalid/compatible-mode/v1",
                dashscope_api_key="synthetic-token-value",
                dashscope_embedding_model="replacement-model",
                dashscope_embedding_dim=2,
            )
        )
        self.addCleanup(backend.close)
        self.assertIsInstance(backend.embedder, EmbeddingClient)
        self.assertEqual(backend.embedder.model, "replacement-model")

    def test_live_http_query_returns_only_ranked_candidates(self) -> None:
        store = ProblemStore(":memory:")
        self.addCleanup(store.close)
        spec = EmbeddingIndexSpec("replacement-model", 2)
        store.prepare_embedding_writes(spec)
        store.add_problem(
            StoredProblem(
                source="synthetic",
                external_id="problem-1",
                title="ranked candidate",
                url=None,
                statement="candidate statement",
                embedding=[1.0, 0.0],
                content_hash="b" * 64,
                source_updated_at="2026-08-14T00:00:00.000Z",
            ),
            index_spec=spec,
        )
        opener = _Opener({"synthetic query": [1.0, 0.0]})
        embedder = EmbeddingClient(
            base_url="https://provider.invalid/compatible-mode/v1",
            api_key="synthetic-token-value",
            model="replacement-model",
            dimensions=2,
            opener=opener,
        )
        service = AnklangService(_config(), UpstreamSearchBackend(store, embedder))
        harness = _Harness(service)
        self.addCleanup(harness.close)
        status, payload = harness.request("/api/v2/checks/similarity", _request())
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {"apiVersion", "contentHash", "checkedAt", "completion", "candidates"},
        )
        self.assertEqual(payload["completion"]["status"], "complete")
        self.assertEqual(payload["candidates"][0]["externalId"], "problem-1")
        self.assertEqual(opener.calls, 1)


if __name__ == "__main__":
    unittest.main()
