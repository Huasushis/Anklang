"""ProblemStore 的单元测试：增删查、去重、embedding 序列化往返、增量游标读写。

全部使用 ":memory:" 内存数据库，不落盘、不做真实网络调用。
"""
from __future__ import annotations

import unittest

from anklang.store import ProblemStore


class ProblemStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.addCleanup(self.store.close)

    def test_add_and_count(self) -> None:
        inserted = self.store.add_problem(
            source="example_static",
            external_id="demo-1",
            title="题目一",
            statement="题面内容",
            content_hash="h1",
        )
        self.assertTrue(inserted)
        self.assertEqual(self.store.count(), 1)

    def test_dedupe_by_source_and_external_id(self) -> None:
        first = self.store.add_problem(
            source="s", external_id="1", title="t1", statement="body1", content_hash="h1"
        )
        again = self.store.add_problem(
            source="s", external_id="1", title="t1-changed", statement="body2", content_hash="h2"
        )
        self.assertTrue(first)
        self.assertFalse(again)
        self.assertEqual(self.store.count(), 1)
        # 已存在的记录不会被"改名"覆盖，第一次写入的内容保留原样。
        stored = self.store.iter_all()[0]
        self.assertEqual(stored.title, "t1")

    def test_different_sources_with_same_external_id_do_not_collide(self) -> None:
        self.store.add_problem(source="a", external_id="1", title="t", statement="x", content_hash="h")
        self.store.add_problem(source="b", external_id="1", title="t", statement="x", content_hash="h")
        self.assertEqual(self.store.count(), 2)

    def test_embedding_blob_roundtrip(self) -> None:
        vector = [0.1, -0.2, 0.3, 0.0, 12.5]
        self.store.add_problem(
            source="s",
            external_id="1",
            title="t",
            statement="body",
            content_hash="h",
            embedding=vector,
        )
        problems = self.store.iter_all()
        self.assertEqual(len(problems), 1)
        restored = problems[0].embedding
        self.assertIsNotNone(restored)
        assert restored is not None  # 帮类型检查器确认非空
        self.assertEqual(len(restored), len(vector))
        for expected, actual in zip(vector, restored):
            self.assertAlmostEqual(expected, actual, places=5)

    def test_missing_embedding_stays_none_until_backfilled(self) -> None:
        self.store.add_problem(source="s", external_id="1", title="t", statement="body", content_hash="h")
        problems = self.store.iter_all()
        self.assertIsNone(problems[0].embedding)

        missing = self.store.iter_missing_embeddings()
        self.assertEqual(len(missing), 1)
        self.store.update_embedding(missing[0].id, [1.0, 2.0, 3.0])

        self.assertEqual(len(self.store.iter_missing_embeddings()), 0)
        refreshed = self.store.iter_all()[0]
        self.assertIsNotNone(refreshed.embedding)

    def test_cursor_get_and_set(self) -> None:
        self.assertIsNone(self.store.get_cursor("example_static"))
        self.store.set_cursor("example_static", "2026-01-01")
        self.assertEqual(self.store.get_cursor("example_static"), "2026-01-01")
        self.store.set_cursor("example_static", "2026-02-01")
        self.assertEqual(self.store.get_cursor("example_static"), "2026-02-01")

    def test_cursors_are_isolated_per_source(self) -> None:
        self.store.set_cursor("source-a", "2026-01-01")
        self.store.set_cursor("source-b", "2026-06-01")
        self.assertEqual(self.store.get_cursor("source-a"), "2026-01-01")
        self.assertEqual(self.store.get_cursor("source-b"), "2026-06-01")

    def test_batch_insert_counts_only_new(self) -> None:
        count = self.store.add_problems_batch(
            [
                {"source": "s", "external_id": "1", "title": "t1", "statement": "a", "content_hash": "h1"},
                {"source": "s", "external_id": "2", "title": "t2", "statement": "b", "content_hash": "h2"},
                {"source": "s", "external_id": "1", "title": "t1-dup", "statement": "a2", "content_hash": "h1b"},
            ]
        )
        self.assertEqual(count, 2)
        self.assertEqual(self.store.count(), 2)


if __name__ == "__main__":
    unittest.main()
