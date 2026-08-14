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
from .sources import (
    RawProblem,
    SourceContractError,
    discover_source_modules,
    is_valid_source_updated_at,
    validate_raw_problem,
)
from .store import EmbeddingIndexSpec, IndexMetadataError, ProblemStore, StoredProblem
from .text_normalize import content_hash_of, normalize_statement
from .vectormath import validate_embedding


@dataclass
class IngestSummary:
    """一次 ingest_once() 调用的统计结果，用于命令行打印和测试断言。"""

    per_source_fetched: dict[str, int] = field(default_factory=dict)
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    source_failures: int = 0
    embedding_failures: int = 0

    @property
    def fetched(self) -> int:
        return sum(self.per_source_fetched.values())


def ingest_once(store: ProblemStore, embedder: EmbeddingClient | None) -> IngestSummary:
    """跑一轮全部源插件的抓取入库，返回统计结果。可以反复调用，调用间隔由调用方
    决定（命令行一次性调用，或者后台线程定时调用）。
    """
    summary = IngestSummary()
    index_spec: EmbeddingIndexSpec | None = None
    if embedder is not None:
        index_spec = EmbeddingIndexSpec(
            model=embedder.model,
            dimensions=embedder.dimensions,
        )
        # 在发现来源、读取题面和调用外部模型前拒绝旧向量或规格冲突。
        store.prepare_embedding_writes(index_spec)
    else:
        store.prepare_keyword_writes()
    for module in discover_source_modules():
        source_name = module.SOURCE_NAME
        stored_since = store.get_cursor(source_name)
        # 旧版本曾允许来源自定义任意游标。遇到这种旧值时做一次全量读取，
        # 成功后再用规范 UTC 时间替换它。
        fetch_since = (
            stored_since
            if stored_since is None or is_valid_source_updated_at(stored_since)
            else None
        )
        try:
            raw_problems = module.fetch_new_problems(fetch_since)
            selected, ambiguous_count = _select_latest_versions(raw_problems)
        except Exception:
            # 一个来源失效不应阻断其他来源；异常内容可能带有私有地址或响应摘要，
            # 因此这里只记录固定计数，不输出原异常。
            summary.source_failures += 1
            continue
        summary.per_source_fetched[source_name] = len(raw_problems)
        summary.skipped += ambiguous_count
        # 只要本批有题目没有更新时间，就不能用其他题目的时间代表整批进度，
        # 否则来源下次按时间筛选时可能漏掉这类题目。
        may_advance_cursor = ambiguous_count == 0 and all(
            raw.updated_at is not None for raw in raw_problems
        )

        latest_updated_at: str | None = None
        for raw in raw_problems:
            if raw.updated_at is not None and (
                latest_updated_at is None or raw.updated_at > latest_updated_at
            ):
                latest_updated_at = raw.updated_at

        for raw in selected:
            statement = normalize_statement(raw.statement)
            content_hash = content_hash_of(statement)
            existing = store.get_problem(source_name, raw.external_id)
            embedding: list[float] | None = None
            if embedder is not None and _needs_embedding(
                existing,
                raw,
                content_hash,
            ):
                try:
                    embedding = embedder.embed_one(statement)
                    assert index_spec is not None
                    embedding = validate_embedding(
                        embedding,
                        expected_dimensions=index_spec.dimensions,
                    )
                except (EmbeddingError, ValueError):
                    # 向量服务暂不可用时仍写入题目；关键词检索立即可用。
                    # 来源后续再次返回该题时，会重试缺失向量。
                    summary.embedding_failures += 1
                    embedding = None
            write_result = store.add_problem(
                source=source_name,
                external_id=raw.external_id,
                title=raw.title,
                url=raw.url,
                statement=statement,
                embedding=embedding,
                index_spec=index_spec if embedding is not None else None,
                content_hash=content_hash,
                source_updated_at=raw.updated_at,
            )
            if write_result == "inserted":
                summary.inserted += 1
            elif write_result == "updated":
                summary.updated += 1
            elif write_result == "skipped":
                summary.skipped += 1
                may_advance_cursor = False
            else:
                summary.unchanged += 1

        if (
            may_advance_cursor
            and latest_updated_at is not None
            and (fetch_since is None or latest_updated_at > fetch_since)
        ):
            store.set_cursor_if_current(
                source_name,
                expected_value=stored_since,
                next_value=latest_updated_at,
            )
    return summary


def _select_latest_versions(
    raw_problems: object,
) -> tuple[list[RawProblem], int]:
    """每个题号只保留明确较新的版本；无法判断先后的冲突整组跳过。"""
    if not isinstance(raw_problems, list):
        raise SourceContractError("来源返回值必须是题目列表。")

    selected: dict[str, RawProblem] = {}
    ambiguous: set[str] = set()
    for raw_value in raw_problems:
        raw = validate_raw_problem(raw_value)
        if raw.external_id in ambiguous:
            continue
        previous = selected.get(raw.external_id)
        if previous is None:
            selected[raw.external_id] = raw
            continue
        choice = _newer_problem(previous, raw)
        if choice is None:
            selected.pop(raw.external_id, None)
            ambiguous.add(raw.external_id)
        else:
            selected[raw.external_id] = choice
    return list(selected.values()), len(ambiguous)


def _newer_problem(first: RawProblem, second: RawProblem) -> RawProblem | None:
    if first.updated_at is not None and second.updated_at is not None:
        if second.updated_at > first.updated_at:
            return second
        if second.updated_at < first.updated_at:
            return first
    if _same_source_content(first, second):
        if first.updated_at is None and second.updated_at is not None:
            return second
        return first
    return None


def _same_source_content(first: RawProblem, second: RawProblem) -> bool:
    return (
        first.title == second.title
        and first.statement == second.statement
        and first.url == second.url
    )


def _source_fields_can_be_updated(
    existing: StoredProblem,
    raw: RawProblem,
    content_hash: str,
) -> bool:
    source_fields_changed = (
        existing.title != raw.title
        or existing.url != raw.url
        or existing.content_hash != content_hash
    )
    if not source_fields_changed:
        return True
    return (
        raw.updated_at is not None
        and existing.source_updated_at is not None
        and raw.updated_at > existing.source_updated_at
    )


def _needs_embedding(
    existing: StoredProblem | None,
    raw: RawProblem,
    content_hash: str,
) -> bool:
    if existing is None:
        return True
    if not _source_fields_can_be_updated(existing, raw, content_hash):
        return False
    return existing.embedding is None or existing.content_hash != content_hash


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
    try:
        summary = ingest_once(store, embedder)
    except IndexMetadataError:
        sys.stderr.write(
            "本地向量索引未通过当前写入门禁，任务没有继续；现有数据未被清空。"
            "请按文档使用新的数据库路径重建。\n"
        )
        return 2
    finally:
        store.close()
    sys.stderr.write(
        "抓取完成："
        f"共发现 {summary.fetched} 条，新入库 {summary.inserted} 条，"
        f"更新 {summary.updated} 条，未变化 {summary.unchanged} 条，"
        f"因版本不明确跳过 {summary.skipped} 条，"
        f"来源失败 {summary.source_failures} 个，"
        f"embedding 失败 {summary.embedding_failures} 条。\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
