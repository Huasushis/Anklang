"""本地引擎 embedding 补算（backfill）入口。

入库时如果 embedding 服务暂时不可用（或者一开始就没配置 DASHSCOPE_API_KEY），
anklang/ingest.py 会先把 embedding 列留空（None）入库，不阻塞抓取流程。这个模块
用来在 embedding 服务恢复可用之后，把这些"还没算出向量"的题目补算一遍。

用法：
  python -m anklang.backfill
"""
from __future__ import annotations

import sys

from .config import ConfigError, load_config
from .embedding import EmbeddingClient, EmbeddingError
from .store import ProblemStore


def backfill_missing_embeddings(store: ProblemStore, embedder: EmbeddingClient) -> tuple[int, int]:
    """对 store 里 embedding 为空的题目重新计算向量并写回。返回 (成功补算数, 失败数)。"""
    missing = store.iter_missing_embeddings()
    succeeded = 0
    failed = 0
    for problem in missing:
        try:
            embedding = embedder.embed_one(problem.statement)
        except EmbeddingError:
            failed += 1
            continue
        if store.update_embedding(
            problem.id,
            embedding,
            expected_content_hash=problem.content_hash,
        ):
            succeeded += 1
        else:
            # 计算期间题面已经更新，旧向量不能写回新题面。
            failed += 1
    return succeeded, failed


def main() -> int:
    try:
        config = load_config()
    except ConfigError as error:
        sys.stderr.write(f"配置错误：{error}\n")
        return 2
    if not (config.dashscope_api_key and config.dashscope_base_url):
        sys.stderr.write("未配置 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL，无法补算 embedding。\n")
        return 2

    store = ProblemStore(config.local_db_path)
    embedder = EmbeddingClient(
        base_url=config.dashscope_base_url,
        api_key=config.dashscope_api_key,
        model=config.dashscope_embedding_model,
        dimensions=config.dashscope_embedding_dim,
    )
    succeeded, failed = backfill_missing_embeddings(store, embedder)
    sys.stderr.write(f"embedding 补算完成：成功 {succeeded} 条，失败 {failed} 条。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
