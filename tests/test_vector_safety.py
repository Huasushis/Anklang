"""Replacement-provider response and vector serialization safety tests."""
from __future__ import annotations

import json
import math
import struct
import unittest
from typing import Any

from anklang.embedding import EmbeddingClient, EmbeddingError

from anklang.vectormath import (
    cosine_similarity,
    pack_embedding,
    unpack_embedding,
    validate_embedding,
)


class _Response:
    def __init__(self, payload: Any) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._raw


class _Opener:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls = 0

    def __call__(self, _request: Any, timeout: float) -> _Response:  # noqa: ARG002
        self.calls += 1
        return _Response(self._payload)


class _RawResponse:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def __enter__(self) -> "_RawResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._raw


class _RawOpener:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.calls = 0

    def __call__(self, _request: Any, timeout: float) -> _RawResponse:  # noqa: ARG002
        self.calls += 1
        return _RawResponse(self._raw)


def _deep_json(marker: str, depth: int = 2_000) -> bytes:
    """直接拼出深层 JSON，避免测试准备阶段自己先触发递归限制。"""
    leaf = json.dumps(marker, ensure_ascii=False)
    return (("[" * depth) + leaf + ("]" * depth)).encode("utf-8")


def _embedding_client(payload: Any, *, dimensions: int = 2) -> EmbeddingClient:
    if isinstance(payload, dict) and "model" not in payload:
        payload = {**payload, "model": "test-model"}
    return EmbeddingClient(
        base_url="https://embedding.test/compatible-mode/v1",
        api_key="test-key",
        model="test-model",
        dimensions=dimensions,
        opener=_Opener(payload),
    )



class EmbeddingResponseSafetyTests(unittest.TestCase):
    def test_rejects_invalid_configured_dimensions(self) -> None:
        for dimensions in (True, 0, -1, 1.5):
            with self.subTest(dimensions=dimensions):
                with self.assertRaises(ValueError):
                    _embedding_client({"data": []}, dimensions=dimensions)  # type: ignore[arg-type]

    def test_rejects_empty_bool_nonfinite_and_wrong_dimension_vectors(self) -> None:
        invalid_vectors = (
            [],
            [True, 0.0],
            [math.nan, 0.0],
            [math.inf, 0.0],
            [-math.inf, 0.0],
            [1.0],
            [1.0, 2.0, 3.0],
        )
        for vector in invalid_vectors:
            with self.subTest(vector=vector):
                client = _embedding_client({"data": [{"embedding": vector, "index": 0}]})
                with self.assertRaises(EmbeddingError):
                    client.embed_one("synthetic input")

    def test_rejects_response_row_count_mismatch(self) -> None:
        client = _embedding_client(
            {"data": [{"embedding": [1.0, 0.0], "index": 0}]}
        )
        with self.assertRaises(EmbeddingError):
            client.embed_batch(["first", "second"])

    def test_requires_response_to_confirm_the_requested_model(self) -> None:
        for response_model in (None, "same-dimension-other-model"):
            with self.subTest(response_model=response_model):
                payload: dict[str, Any] = {
                    "data": [{"embedding": [1.0, 0.0]}]
                }
                if response_model is not None:
                    payload["model"] = response_model
                client = EmbeddingClient(
                    base_url="https://embedding.test/compatible-mode/v1",
                    api_key="test-key",
                    model="test-model",
                    dimensions=2,
                    opener=_Opener(payload),
                )
                with self.assertRaisesRegex(
                    EmbeddingError,
                    "响应没有确认请求的模型",
                ):
                    client.embed_one("synthetic input")

    def test_deeply_nested_json_becomes_fixed_embedding_error(self) -> None:
        marker = "上游深层原文不可泄露标记"
        opener = _RawOpener(_deep_json(marker))
        client = EmbeddingClient(
            base_url="https://embedding.test/compatible-mode/v1",
            api_key="test-key",
            model="test-model",
            dimensions=2,
            opener=opener,
        )

        with self.assertRaises(EmbeddingError) as caught:
            client.embed_one("synthetic input")

        # Python 的 JSON 解析器版本不同：有的会在深层结构处直接停止，有的能解析
        # 完成后再由响应结构检查拒绝。两条路径都必须只返回固定说明。
        self.assertIn(
            str(caught.exception),
            {
                "百炼 embedding 响应不是有效 JSON。",
                "百炼 embedding 响应条数与请求不一致。",
                "百炼 embedding 响应没有确认请求的模型。",
            },
        )
        self.assertNotIn(marker, str(caught.exception))
        self.assertEqual(opener.calls, 1)

    def test_rejects_non_integer_duplicate_or_incomplete_indices(self) -> None:
        invalid_indices = (
            [0, True],
            [0, "1"],
            [0, 1.0],
            [0, 0],
            [0, 2],
            [-1, 1],
            [None, 1],
        )
        for indices in invalid_indices:
            with self.subTest(indices=indices):
                client = _embedding_client(
                    {
                        "data": [
                            (
                                {"embedding": [1.0, 0.0]}
                                if indices[0] is None
                                else {
                                    "embedding": [1.0, 0.0],
                                    "index": indices[0],
                                }
                            ),
                            {"embedding": [0.0, 1.0], "index": indices[1]},
                        ]
                    }
                )
                with self.assertRaises(EmbeddingError):
                    client.embed_batch(["first", "second"])

    def test_uses_valid_indices_to_restore_input_order(self) -> None:
        client = _embedding_client(
            {
                "data": [
                    {"embedding": [0.0, 1.0], "index": 1},
                    {"embedding": [1.0, 0.0], "index": 0},
                ]
            }
        )
        self.assertEqual(
            client.embed_batch(["first", "second"]),
            [[1.0, 0.0], [0.0, 1.0]],
        )

    def test_missing_indices_keep_response_order_for_compatible_services(self) -> None:
        client = _embedding_client(
            {
                "data": [
                    {"embedding": [1.0, 0.0]},
                    {"embedding": [0.0, 1.0]},
                ]
            }
        )
        self.assertEqual(
            client.embed_batch(["first", "second"]),
            [[1.0, 0.0], [0.0, 1.0]],
        )


class VectorStorageSafetyTests(unittest.TestCase):
    def test_validation_rejects_empty_bool_nonfinite_and_wrong_dimensions(self) -> None:
        for vector in ([], [True], [math.nan], [math.inf], [-math.inf]):
            with self.subTest(vector=vector):
                with self.assertRaises(ValueError):
                    validate_embedding(vector)
                with self.assertRaises(ValueError):
                    pack_embedding(vector)
        with self.assertRaises(ValueError):
            validate_embedding([1.0, 2.0], expected_dimensions=3)

    def test_unpack_rejects_empty_misaligned_and_nonfinite_storage(self) -> None:
        invalid_blobs = (
            b"",
            b"\x00",
            struct.pack("<f", math.nan),
            struct.pack("<f", math.inf),
        )
        for blob in invalid_blobs:
            with self.subTest(blob=blob):
                with self.assertRaises(ValueError):
                    unpack_embedding(blob)

    def test_cosine_similarity_treats_invalid_vectors_as_unusable(self) -> None:
        invalid_pairs = (
            ([], []),
            ([1.0], [1.0, 0.0]),
            ([True, 0.0], [1.0, 0.0]),
            ([math.nan, 0.0], [1.0, 0.0]),
            ([math.inf, 0.0], [1.0, 0.0]),
        )
        for first, second in invalid_pairs:
            with self.subTest(first=first, second=second):
                self.assertEqual(cosine_similarity(first, second), 0.0)

    def test_cosine_similarity_avoids_overflow_for_large_finite_values(self) -> None:
        first = [1e300, 2e300]
        second = [2e300, 4e300]
        self.assertAlmostEqual(cosine_similarity(first, second), 1.0, places=12)




if __name__ == "__main__":
    unittest.main()
