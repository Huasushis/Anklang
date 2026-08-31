from __future__ import annotations

import json
import unittest
import urllib.error
from typing import Any

from anklang.yuantiji import YuantijiClient


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.raw[:limit]


class YuantijiClientTests(unittest.TestCase):
    def test_search_projects_public_fields_and_bounds_statement(self) -> None:
        requests: list[object] = []
        long_statement = "😀" * 16_001

        def opener(request: object, *, timeout: float) -> _Response:
            self.assertEqual(timeout, 12.0)
            requests.append(request)
            return _Response(
                {
                    "results": [
                        {
                            "uid": "synthetic-1",
                            "title": "Synthetic sum",
                            "src": "synthetic-oj",
                            "rr": 0.875,
                            "url": "https://example.invalid/problem/1",
                            "original": long_statement,
                        }
                    ]
                }
            )

        client = YuantijiClient(
            "https://yuantiji.example.invalid",
            timeout_seconds=12,
            minimum_interval_seconds=0,
            max_retries=0,
            opener=opener,
        )
        result = client.search("synthetic query", 3, rerank=True)

        self.assertFalse(result.partial)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate["externalId"], "synthetic-1")
        self.assertEqual(candidate["similarity"], 0.875)
        self.assertEqual(candidate["metadata"], {"search_provider": "yuantiji"})
        self.assertEqual(candidate["statement"], "😀" * 16_000)
        self.assertTrue(candidate["statementTruncated"])

        request = requests[0]
        body = json.loads(getattr(request, "data").decode("utf-8"))
        self.assertEqual(
            body,
            {
                "query": "synthetic query",
                "k": 3,
                "rewrite": False,
                "skip_short": True,
                "sources": [],
                "rerank": True,
            },
        )

    def test_transient_http_failure_is_retried_once(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        def opener(request: object, *, timeout: float) -> _Response:
            nonlocal attempts
            del timeout
            attempts += 1
            if attempts == 1:
                raise urllib.error.HTTPError(
                    getattr(request, "full_url"), 503, "synthetic", {}, None
                )
            return _Response({"results": []})

        client = YuantijiClient(
            "https://yuantiji.example.invalid",
            minimum_interval_seconds=0,
            max_retries=1,
            opener=opener,
            sleeper=sleeps.append,
        )

        result = client.search("synthetic query", 1)

        self.assertEqual(result.candidates, [])
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [1.0])


if __name__ == "__main__":
    unittest.main()
