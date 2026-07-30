"""源插件调度：发现 anklang/sources/ 下的全部源插件，依次抓取新题、规范化、
（若配置了 embedding）计算向量，去重后写入本地题库（ProblemStore）。

用法：
  python -m anklang.ingest     # 跑一次全部源插件的抓取入库，打印一行统计摘要后退出

本模块只提供 ingest_once() 这一次性的"跑一轮"函数，不自带循环/调度——"多久跑
一次"由外层决定，可以是：
  - 上面这条命令行，配合系统的定时任务（cron / 任务计划程序）周期执行；
  - anklang.server 里由 ANKLANG_INGEST_ENABLED 开关控制的后台线程（见 server.py
    的 _start_background_ingest），实现"服务运行时自己按固定间隔抓取"的效果。
两种方式共用同一个 ingest_once()，行为完全一致，不会出现"命令行跑一遍和后台线程
跑一遍逻辑不一样"的分裂。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from .config import ConfigError, load_config
from .embedding import EmbeddingClient, EmbeddingError
from .sources import discover_source_modules
from .store import ProblemStore
from .text_normalize import content_hash_of, normalize_statement


@dataclass
class IngestSummary:
    """一次 ingest_once() 调用的统计结果，用于命令行打印和测试断言。"""

    per_source_fetched: dict[str, int] = field(default_factory=dict)
    inserted: int = 0
    duplicates: int = 0
    embedding_failures: int = 0

    @property
    def fetched(self) -> int:
        return sum(self.per_source_fetched.values())


def ingest_once(store: ProblemStore, embedder: EmbeddingClient | None) -> IngestSummary:
    """跑一轮全部源插件的抓取入库，返回统计结果。可以反复调用，调用间隔由调用方
    决定（命令行一次性调用，或者后台线程定时调用）。
    """
    summary = IngestSummary()
    for module in discover_source_modules():
        source_name = module.SOURCE_NAME
        since = store.get_cursor(source_name)
        raw_problems = module.fetch_new_problems(since)
        summary.per_source_fetched[source_name] = len(raw_problems)

        latest_updated_at: str | None = None
        for raw in raw_problems:
            statement = normalize_statement(raw.statement)
            embedding: list[float] | None = None
            if embedder is not None:
                try:
                    embedding = embedder.embed_one(statement)
                except EmbeddingError:
                    # embedding 服务暂不可用时先存 embedding=None，之后用
                    # anklang.backfill 补算，不影响这道题先入库。
                    summary.embedding_failures += 1
                    embedding = None
            inserted = store.add_problem(
                source=source_name,
                external_id=raw.external_id,
                title=raw.title,
                url=raw.url,
                statement=statement,
                embedding=embedding,
                content_hash=content_hash_of(statement),
            )
            if inserted:
                summary.inserted += 1
            else:
                summary.duplicates += 1
            if raw.updated_at is not None and (
                latest_updated_at is None or raw.updated_at > latest_updated_at
            ):
                latest_updated_at = raw.updated_at

        if latest_updated_at is not None:
            store.set_cursor(source_name, latest_updated_at)
    return summary


def main() -> int:
    try:
        config = load_config()
    except ConfigError as error:
        sys.stderr.write(f"配置错误：{error}\n")
        return 2

    store = ProblemStore(config.local_db_path)
    embedder: EmbeddingClient | None = None
    if config.dashscope_api_key and config.dashscope_base_url:
        embedder = EmbeddingClient(
            base_url=config.dashscope_base_url,
            api_key=config.dashscope_api_key,
            model=config.dashscope_embedding_model,
            dimensions=config.dashscope_embedding_dim,
        )
    summary = ingest_once(store, embedder)
    sys.stderr.write(
        "抓取完成："
        f"共发现 {summary.fetched} 条，新入库 {summary.inserted} 条，"
        f"重复跳过 {summary.duplicates} 条，embedding 失败 {summary.embedding_failures} 条。\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
