from __future__ import annotations

import json
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from anklang.config import AppConfig
from anklang.http_api import AnklangService, make_handler
from anklang.sources import RawProblem
from anklang.store import ProblemStore
from ui.server import UpstreamSearchBackend, start_background_ingest


class _Embedder:
    model = "replacement-model"
    dimensions = 2

    def embed_one(self, text: str) -> list[float]:
        vectors = {
            "first scheduler statement": [1.0, 0.0],
            "second scheduler statement": [0.0, 1.0],
            "conflicting scheduler statement": [0.7, 0.7],
            "scheduler query": [0.0, 1.0],
        }
        return vectors[text]


class LiveSchedulerEndToEndTests(unittest.TestCase):
    def test_emitted_update_becomes_searchable_without_rebuild_or_restart(self) -> None:
        store = ProblemStore(":memory:")
        embedder = _Embedder()
        backend = UpstreamSearchBackend(store, embedder)  # type: ignore[arg-type]
        config = AppConfig(
            port=8730,
            service_token=None,
            search_k=5,
            minimum_similarity=0.0,
            local_db_path=":memory:",
            ingest_enabled=True,
            ingest_interval_seconds=0.02,
        )
        service = AnklangService(config, backend)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()
        stop_event = threading.Event()
        state = {"mode": "empty"}
        state_lock = threading.Lock()

        def fetch_new_problems(_since: str | None) -> list[RawProblem]:
            with state_lock:
                mode = state["mode"]
            if mode == "empty":
                return []
            if mode == "first":
                return [
                    RawProblem(
                        external_id="scheduler-id",
                        title="scheduler version one",
                        statement="first scheduler statement",
                        updated_at="2026-08-14T00:00:00.000Z",
                    )
                ]
            if mode == "second":
                return [
                    RawProblem(
                        external_id="scheduler-id",
                        title="scheduler version two",
                        statement="second scheduler statement",
                        updated_at="2026-08-14T00:00:01.000Z",
                    )
                ]
            return [
                RawProblem(
                    external_id="scheduler-id",
                    title="scheduler conflict",
                    statement="conflicting scheduler statement",
                    updated_at="2026-08-14T00:00:01.000Z",
                )
            ]

        source = SimpleNamespace(
            SOURCE_NAME="live-scheduler-source",
            fetch_new_problems=fetch_new_problems,
        )
        with patch("anklang.ingest.discover_source_modules", return_value=[source]):
            ingest_thread = start_background_ingest(backend, config, stop_event)
            try:
                with state_lock:
                    state["mode"] = "first"
                self._wait_until(lambda: store.count() == 1)
                first = store.get_problem("live-scheduler-source", "scheduler-id")
                self.assertEqual(first.title, "scheduler version one")  # type: ignore[union-attr]

                with state_lock:
                    state["mode"] = "second"
                self._wait_until(
                    lambda: self._candidate_title(httpd.server_port)
                    == "scheduler version two"
                )

                with state_lock:
                    state["mode"] = "conflict"
                time.sleep(0.08)
                self.assertEqual(store.count(), 1)
                self.assertEqual(
                    self._candidate_title(httpd.server_port),
                    "scheduler version two",
                )
                self.assertEqual(
                    store.get_cursor("live-scheduler-source"),
                    "2026-08-14T00:00:01.000Z",
                )
            finally:
                stop_event.set()
                ingest_thread.join(timeout=2)
                httpd.shutdown()
                httpd.server_close()
                http_thread.join(timeout=2)
                store.close()

    @staticmethod
    def _wait_until(predicate: Any) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("live scheduler result did not become observable")

    @staticmethod
    def _candidate_title(port: int) -> str | None:
        payload = {
            "apiVersion": "2",
            "requestId": "77777777-7777-4777-8777-777777777777",
            "contentHash": "7" * 64,
            "problem": {
                "title": "synthetic scheduler query",
                "type": "traditional",
                "tagIds": ["synthetic"],
                "basicStatement": "scheduler query",
            },
        }
        connection = HTTPConnection("127.0.0.1", port, timeout=1)
        connection.request(
            "POST",
            "/api/v2/checks/similarity",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        if response.status != 200 or not body.get("candidates"):
            return None
        self_candidate = body["candidates"][0]
        return self_candidate["title"]


if __name__ == "__main__":
    unittest.main()
