"""源插件框架的端到端测试：example_static 源经框架发现、抓取、入库后，store 里能
查到；增量游标推进后不会重复拉取。不做真实网络调用——example_static 本身就是读
本地 JSON 文件的示例源，不依赖任何外部服务。
"""
from __future__ import annotations

import unittest

from anklang.ingest import ingest_once
from anklang.sources import discover_source_modules
from anklang.sources.example_static import SOURCE_NAME, fetch_new_problems
from anklang.store import ProblemStore


class ExampleStaticSourceTests(unittest.TestCase):
    def test_fetch_without_since_returns_all(self) -> None:
        problems = fetch_new_problems(since=None)
        self.assertGreaterEqual(len(problems), 3)
        external_ids = {p.external_id for p in problems}
        self.assertIn("demo-1001", external_ids)

    def test_fetch_with_since_filters_older_items(self) -> None:
        all_problems = fetch_new_problems(since=None)
        latest = max(p.updated_at for p in all_problems if p.updated_at)
        newer = fetch_new_problems(since=latest)
        self.assertEqual(newer, [])

    def test_fetch_with_since_before_first_item_returns_all(self) -> None:
        problems = fetch_new_problems(since="2000-01-01")
        self.assertEqual(len(problems), len(fetch_new_problems(since=None)))


class SourceDiscoveryTests(unittest.TestCase):
    def test_example_static_is_discovered(self) -> None:
        modules = discover_source_modules()
        names = {module.SOURCE_NAME for module in modules}
        self.assertIn(SOURCE_NAME, names)

    def test_discovered_modules_expose_the_plugin_contract(self) -> None:
        for module in discover_source_modules():
            self.assertTrue(hasattr(module, "SOURCE_NAME"))
            self.assertTrue(callable(getattr(module, "fetch_new_problems", None)))


class IngestFrameworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProblemStore(":memory:")
        self.addCleanup(self.store.close)

    def test_ingest_once_loads_example_static_into_store(self) -> None:
        summary = ingest_once(self.store, embedder=None)
        self.assertGreater(summary.inserted, 0)
        self.assertIn(SOURCE_NAME, summary.per_source_fetched)

        problems = self.store.iter_all()
        sources = {p.source for p in problems}
        self.assertIn(SOURCE_NAME, sources)
        # 没有传 embedder，走的是"embedding 服务暂不可用"的降级分支，不应该报错，
        # 也不应该产生向量——留给之后的 backfill 补算。
        self.assertTrue(all(p.embedding is None for p in problems if p.source == SOURCE_NAME))

    def test_cursor_prevents_refetch_of_same_items(self) -> None:
        first = ingest_once(self.store, embedder=None)
        second = ingest_once(self.store, embedder=None)
        self.assertGreater(first.inserted, 0)
        # 游标已经推进到第一轮抓到的最大 updated_at，第二轮不应该再拉到同一批数据。
        self.assertEqual(second.per_source_fetched.get(SOURCE_NAME, 0), 0)
        self.assertEqual(second.inserted, 0)

    def test_ingest_is_idempotent_even_without_cursor_advantage(self) -> None:
        first = ingest_once(self.store, embedder=None)
        # 模拟"游标被重置 / 来源不支持增量"的情况：强制回到最早之前，让框架重新
        # 拉一遍全量，验证即使这样也不会产生重复记录（靠 (source, external_id) 去重）。
        self.store.set_cursor(SOURCE_NAME, "2000-01-01")
        second = ingest_once(self.store, embedder=None)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.duplicates, first.inserted)

    def test_cursor_advances_after_ingest(self) -> None:
        self.assertIsNone(self.store.get_cursor(SOURCE_NAME))
        ingest_once(self.store, embedder=None)
        self.assertIsNotNone(self.store.get_cursor(SOURCE_NAME))


if __name__ == "__main__":
    unittest.main()
