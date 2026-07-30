"""向量打包/解包与余弦相似度的单元测试。"""
from __future__ import annotations

import unittest

from anklang.vectormath import cosine_similarity, pack_embedding, unpack_embedding


class VectorMathTests(unittest.TestCase):
    def test_pack_unpack_roundtrip(self) -> None:
        vector = [0.5, -1.25, 3.0, 0.0, 100.0]
        blob = pack_embedding(vector)
        self.assertIsInstance(blob, bytes)
        restored = unpack_embedding(blob)
        self.assertEqual(len(restored), len(vector))
        for expected, actual in zip(vector, restored):
            self.assertAlmostEqual(expected, actual, places=5)

    def test_identical_vectors_have_similarity_one(self) -> None:
        vector = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(cosine_similarity(vector, vector), 1.0, places=6)

    def test_orthogonal_vectors_have_similarity_zero(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_opposite_vectors_have_similarity_negative_one(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0, places=6)

    def test_mismatched_length_returns_zero(self) -> None:
        self.assertEqual(cosine_similarity([1.0, 2.0], [1.0]), 0.0)

    def test_empty_vectors_return_zero(self) -> None:
        self.assertEqual(cosine_similarity([], []), 0.0)

    def test_zero_vector_returns_zero(self) -> None:
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_scaling_does_not_change_similarity(self) -> None:
        a = [1.0, 2.0, 3.0]
        b = [2.0, 4.0, 6.0]  # a 的 2 倍，方向相同，余弦相似度应仍是 1
        self.assertAlmostEqual(cosine_similarity(a, b), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
