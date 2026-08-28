"""Anklang 单题入库 HTTP 契约；只使用合成题面和回环连接。"""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from typing import Any

from anklang.config import AppConfig
from anklang.http_api import (
    AnklangHTTPServer,
    AnklangService,
    ServiceRuntime,
    make_handler,
)
from anklang.provider import ProviderRegistry
from anklang.store import EmbeddingIndexSpec, ProblemStore
from anklang.text_normalize import content_hash_of, normalize_statement
from ui.server import UpstreamSearchBackend

_TOKEN = "synthetic-service-token-abcdef"
_PATH = "/api/v1/index/problems"


class _Embedder:
    model = "synthetic-model"
    dimensions = 2

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0] if "alpha" in text else [0.0, 1.0]


class _FailingEmbedder(_Embedder):
    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        raise RuntimeError("synthetic provider failure")


class _BlockingEmbedder(_Embedder):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        self.entered.set()
        self.release.wait(2)
        return [1.0, 0.0]


class _Harness:
    def __init__(self, embedder: Any | None = None) -> None:
        self.store = ProblemStore(":memory:")
        self.embedder = _Embedder() if embedder is None else embedder
        backend = UpstreamSearchBackend(
            self.store, ProviderRegistry(initial=self.embedder)
        )
        config = AppConfig(
            port=8730,
            service_token=_TOKEN,
            search_k=5,
            minimum_similarity=0.0,
            local_db_path=":memory:",
            max_in_flight_checks=1,
        )
        self.service = AnklangService(config, backend)
        self.runtime = ServiceRuntime(config.max_in_flight_checks)
        self.server = AnklangHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, self.runtime)
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.backend = backend

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | bytes | str | None = None,
        *,
        authorized: bool = True,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if isinstance(payload, dict):
            body: bytes | None = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authorized:
            headers["Authorization"] = f"Bearer {_TOKEN}"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            payload_result = json.loads(raw.decode("utf-8")) if raw else {}
            response_headers = {
                name.lower(): value for name, value in response.getheaders()
            }
            return response.status, payload_result, response_headers
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.backend.close()


def _upsert_payload(
    *,
    external_id: str = "synthetic-problem-1",
    title: str = "synthetic alpha title",
    statement: str = "synthetic alpha statement",
    updated_at: str = "2026-08-28T00:00:00.000Z",
) -> dict[str, Any]:
    return {
        "apiVersion": "1",
        "requestId": "44444444-4444-4444-8444-444444444444",
        "externalId": external_id,
        "updatedAt": updated_at,
        "problem": {"title": title, "basicStatement": statement},
    }


class UpsertContractTests(unittest.TestCase):
    def test_auth_precedes_body_and_extra_fields_are_invalid(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)

        status, payload, headers = harness.request(
            "PUT", _PATH, b"not-json", authorized=False
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(harness.embedder.calls, [])

        invalid = _upsert_payload()
        invalid["extra"] = True
        status, payload, headers = harness.request("PUT", _PATH, invalid)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(headers.get("cache-control"), "no-store")

        invalid = _upsert_payload()
        invalid["problem"]["url"] = "https://example.invalid/synthetic"
        status, payload, _headers = harness.request("PUT", _PATH, invalid)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

        for index, invalid in enumerate(
            (
                _upsert_payload(external_id=" "),
                _upsert_payload(external_id="x" * 201),
                _upsert_payload(updated_at="2026-08-28T00:00:00+08:00"),
                _upsert_payload(title=""),
                _upsert_payload(statement="\t"),
                _upsert_payload(statement="x" * 500_001),
            )
        ):
            with self.subTest(case=index):
                status, payload, _headers = harness.request("PUT", _PATH, invalid)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

    def test_upsert_shares_bounded_in_flight_admission_with_queries(self) -> None:
        embedder = _BlockingEmbedder()
        harness = _Harness(embedder)
        self.addCleanup(embedder.release.set)
        self.addCleanup(harness.close)
        first_result: list[tuple[int, dict[str, Any], dict[str, str]]] = []
        first = threading.Thread(
            target=lambda: first_result.append(
                harness.request("PUT", _PATH, _upsert_payload())
            )
        )
        first.start()
        self.assertTrue(embedder.entered.wait(2))

        status, payload, headers = harness.request("PUT", _PATH, b"not-json")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "SERVICE_BUSY")
        self.assertEqual(headers.get("cache-control"), "no-store")

        embedder.release.set()
        first.join(2)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_result[0][0], 200)

    def test_insert_and_identical_replay_are_strict_and_idempotent(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        request = _upsert_payload()

        status, response, headers = harness.request("PUT", _PATH, request)
        self.assertEqual(status, 200)
        self.assertEqual(
            set(response),
            {
                "apiVersion",
                "requestId",
                "source",
                "externalId",
                "contentHash",
                "outcome",
            },
        )
        self.assertEqual(response["apiVersion"], "1")
        self.assertEqual(response["requestId"], request["requestId"])
        self.assertEqual(response["source"], "urmotiv")
        self.assertEqual(response["externalId"], request["externalId"])
        self.assertEqual(
            response["contentHash"],
            content_hash_of(normalize_statement(request["problem"]["basicStatement"])),
        )
        self.assertEqual(response["outcome"], "inserted")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(len(harness.embedder.calls), 1)

        status, replay, headers = harness.request("PUT", _PATH, request)
        self.assertEqual(status, 200)
        self.assertEqual(replay["outcome"], "unchanged")
        self.assertEqual(replay["contentHash"], response["contentHash"])
        self.assertEqual(len(harness.embedder.calls), 1)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_newer_title_update_reuses_vector_and_content_update_reembeds(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        first = _upsert_payload()
        self.assertEqual(harness.request("PUT", _PATH, first)[0], 200)

        title_update = _upsert_payload(
            title="synthetic alpha title v2",
            updated_at="2026-08-28T00:00:01.000Z",
        )
        status, response, _headers = harness.request("PUT", _PATH, title_update)
        self.assertEqual((status, response["outcome"]), (200, "updated"))
        self.assertEqual(len(harness.embedder.calls), 1)
        stored = harness.store.get_problem("urmotiv", first["externalId"])
        self.assertEqual(stored.title, title_update["problem"]["title"])  # type: ignore[union-attr]
        self.assertEqual(stored.embedding, [1.0, 0.0])  # type: ignore[union-attr]

        content_update = _upsert_payload(
            title="synthetic beta title",
            statement="synthetic beta statement",
            updated_at="2026-08-28T00:00:02.000Z",
        )
        status, response, _headers = harness.request("PUT", _PATH, content_update)
        self.assertEqual((status, response["outcome"]), (200, "updated"))
        self.assertEqual(len(harness.embedder.calls), 2)
        stored = harness.store.get_problem("urmotiv", first["externalId"])
        self.assertEqual(stored.statement, "synthetic beta statement")  # type: ignore[union-attr]
        self.assertEqual(stored.embedding, [0.0, 1.0])  # type: ignore[union-attr]

    def test_stale_update_conflicts_without_overwriting_current_row(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        current = _upsert_payload(
            title="synthetic current title",
            statement="synthetic alpha statement",
            updated_at="2026-08-28T00:00:02.000Z",
        )
        self.assertEqual(harness.request("PUT", _PATH, current)[0], 200)
        stale = _upsert_payload(
            title="synthetic stale title",
            statement="synthetic beta statement",
            updated_at="2026-08-28T00:00:01.000Z",
        )
        status, response, headers = harness.request("PUT", _PATH, stale)
        self.assertEqual(status, 409)
        self.assertEqual(
            response,
            {
                "error": {
                    "code": "STALE_UPDATE",
                    "message": "题目版本已过期或发生冲突。",
                }
            },
        )
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(len(harness.embedder.calls), 1)
        stored = harness.store.get_problem("urmotiv", current["externalId"])
        self.assertEqual(stored.title, current["problem"]["title"])  # type: ignore[union-attr]

    def test_missing_failed_embedding_and_unusable_index_are_unavailable(self) -> None:
        missing = ProblemStore(":memory:")
        missing_backend = UpstreamSearchBackend(missing, None)
        missing_config = AppConfig(
            port=8730,
            service_token=_TOKEN,
            search_k=5,
            minimum_similarity=0.0,
            local_db_path=":memory:",
        )
        missing_service = AnklangService(missing_config, missing_backend)
        missing_server = AnklangHTTPServer(
            ("127.0.0.1", 0), make_handler(missing_service)
        )
        missing_server_thread = threading.Thread(
            target=missing_server.serve_forever, daemon=True
        )
        missing_server_thread.start()
        self.addCleanup(missing_server.shutdown)
        self.addCleanup(missing_server.server_close)
        self.addCleanup(missing_backend.close)
        connection = HTTPConnection("127.0.0.1", missing_server.server_port, timeout=3)
        try:
            body = json.dumps(_upsert_payload()).encode("utf-8")
            connection.request(
                "PUT",
                _PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            result = connection.getresponse()
            payload = json.loads(result.read().decode("utf-8"))
            self.assertEqual(result.status, 503)
            self.assertEqual(payload["error"]["code"], "INDEX_UNAVAILABLE")
            self.assertEqual(result.getheader("Cache-Control"), "no-store")
        finally:
            connection.close()

        failed = _Harness(_FailingEmbedder())
        self.addCleanup(failed.close)
        status, payload, headers = failed.request("PUT", _PATH, _upsert_payload())
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "INDEX_UNAVAILABLE")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(failed.store.count(), 0)

        unusable_store = ProblemStore(":memory:")
        unusable_store.prepare_embedding_writes(EmbeddingIndexSpec("other-model", 2))
        unusable_backend = UpstreamSearchBackend(
            unusable_store, ProviderRegistry(initial=_Embedder())
        )
        unusable_service = AnklangService(missing_config, unusable_backend)
        unusable_server = AnklangHTTPServer(
            ("127.0.0.1", 0), make_handler(unusable_service)
        )
        unusable_server_thread = threading.Thread(
            target=unusable_server.serve_forever, daemon=True
        )
        unusable_server_thread.start()
        self.addCleanup(unusable_server.shutdown)
        self.addCleanup(unusable_server.server_close)
        self.addCleanup(unusable_backend.close)
        connection = HTTPConnection("127.0.0.1", unusable_server.server_port, timeout=3)
        try:
            body = json.dumps(_upsert_payload()).encode("utf-8")
            connection.request(
                "PUT",
                _PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            result = connection.getresponse()
            payload = json.loads(result.read().decode("utf-8"))
            self.assertEqual(result.status, 503)
            self.assertEqual(payload["error"]["code"], "INDEX_UNAVAILABLE")
        finally:
            connection.close()

    def test_method_path_and_existing_query_shapes_remain_compatible(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        inserted = _upsert_payload()
        self.assertEqual(harness.request("PUT", _PATH, inserted)[0], 200)

        for method, path, expected_status in (
            ("GET", _PATH, 405),
            ("POST", _PATH, 405),
            ("DELETE", _PATH, 405),
            ("PUT", "/api/v1/index/unknown", 404),
        ):
            with self.subTest(method=method, path=path):
                status, payload, headers = harness.request(method, path, inserted)
                self.assertEqual(status, expected_status)
                self.assertIn(
                    payload["error"]["code"], {"METHOD_NOT_ALLOWED", "NOT_FOUND"}
                )
                self.assertEqual(headers.get("cache-control"), "no-store")

        query = {
            "apiVersion": "2",
            "requestId": "55555555-5555-4555-8555-555555555555",
            "contentHash": "5" * 64,
            "problem": {
                "title": "synthetic query",
                "type": "traditional",
                "tagIds": ["synthetic"],
                "basicStatement": "synthetic alpha statement",
            },
        }
        status, v2, _headers = harness.request(
            "POST", "/api/v2/checks/similarity", query
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(v2),
            {"apiVersion", "contentHash", "checkedAt", "completion", "candidates"},
        )
        query["apiVersion"] = "1"
        status, v1, _headers = harness.request(
            "POST", "/api/v1/checks/similarity", query
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(v1), {"apiVersion", "contentHash", "checkedAt", "candidates"}
        )


if __name__ == "__main__":
    unittest.main()
