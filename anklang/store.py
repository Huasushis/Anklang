"""本地题库存储：SQLite 单文件（db_path 传 ":memory:" 时是纯内存数据库，供测试用）。

表结构（problems）：
  id              自增主键
  source          来源标识：某个源插件的 SOURCE_NAME（见 anklang/sources/__init__.py），
                  或反向代理场景里不会用到这张表
  external_id     该来源内部的题目编号
  title           题目标题
  url             题目链接，可为空
  statement       规范化后的题面文本（用于关键词检索与候选展示片段）
  embedding       题面的向量表示，序列化成 BLOB（见 anklang.vectormath）；可能为空，
                  表示还没算出向量（embedding 服务暂不可用时的降级状态），
                  用 iter_missing_embeddings + update_embedding 补算
  content_hash    规范化题面文本的 sha256，用于快速判断内容是否变化
  created_at      入库时间（UTC ISO 8601 字符串，带毫秒和 Z 后缀）

(source, external_id) 上有唯一约束，保证同一来源的同一道题不会重复入库——这是
"去重"在存储层的落地方式，调用方（anklang/ingest.py）不需要自己实现去重逻辑。

另有 ingest_cursor 表，记录每个源插件"抓取到哪里了"的增量游标（since 值），每个源
插件只能读写自己那一行，不与其他源共享，这与 docs/plan.md 4.1 节"插件的中间数据
只存在自己的命名空间里"的隔离原则一致。

并发说明：整个 ProblemStore 只持有一个 sqlite3 连接（check_same_thread=False），
所有方法都在同一把进程内锁（self._lock）保护下执行，不追求高并发读写性能——本地
引擎目前是面向"未来自建题库"的框架性实现，量级和并发都远低于需要精细优化的程度；
需要更高并发时再按 docs/plan.md 3.2 节的预案升级到 Postgres。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .vectormath import pack_embedding, unpack_embedding

_SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    statement TEXT NOT NULL,
    embedding BLOB,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE TABLE IF NOT EXISTS ingest_cursor (
    source TEXT PRIMARY KEY,
    since_value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class StoredProblem:
    """从 problems 表读出的一行，embedding 已反序列化成浮点数列表（可能为 None）。"""

    id: int
    source: str
    external_id: str
    title: str
    url: str | None
    statement: str
    embedding: list[float] | None
    content_hash: str
    created_at: str


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class ProblemStore:
    """本地题库的 SQLite 存取封装。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False：ThreadingHTTPServer 每个请求一个线程、后台 ingest
        # 又是另一个线程，都要共用这一个连接；线程安全完全靠 self._lock 保证，不依赖
        # sqlite3 自己的线程检查。
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add_problem(
        self,
        *,
        source: str,
        external_id: str,
        title: str,
        statement: str,
        content_hash: str,
        url: str | None = None,
        embedding: list[float] | None = None,
    ) -> bool:
        """插入一道题；(source, external_id) 已存在则忽略。返回是否真的新插入了一行。"""
        blob = pack_embedding(embedding) if embedding is not None else None
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO problems
                    (source, external_id, title, url, statement, embedding, content_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source, external_id, title, url, statement, blob, content_hash, _utc_now_iso()),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def add_problems_batch(self, problems: Iterable[dict[str, Any]]) -> int:
        """批量插入，每个元素是 add_problem 的关键字参数字典。返回真正新插入的条数
        （已存在的按 (source, external_id) 跳过，不计入）。"""
        inserted = 0
        for problem in problems:
            if self.add_problem(**problem):
                inserted += 1
        return inserted

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM problems").fetchone()
            return int(row["c"])

    def iter_all(self) -> list[StoredProblem]:
        """取出全部题目（本地引擎检索时逐条计算相似度用）。一次性取成列表而不是
        生成器，避免在持锁期间把锁的释放时机和调用方遍历的节奏绑在一起。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source, external_id, title, url, statement, embedding, "
                "content_hash, created_at FROM problems"
            ).fetchall()
        return [_row_to_problem(row) for row in rows]

    def iter_missing_embeddings(self) -> list[StoredProblem]:
        """取出还没算出向量的题目（embedding backfill 用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source, external_id, title, url, statement, embedding, "
                "content_hash, created_at FROM problems WHERE embedding IS NULL"
            ).fetchall()
        return [_row_to_problem(row) for row in rows]

    def update_embedding(self, problem_id: int, embedding: list[float]) -> None:
        blob = pack_embedding(embedding)
        with self._lock:
            self._conn.execute("UPDATE problems SET embedding = ? WHERE id = ?", (blob, problem_id))
            self._conn.commit()

    def get_cursor(self, source: str) -> str | None:
        """读取某个源插件上次抓取到的增量游标（since 值），没有则返回 None（表示
        "第一次抓取"，由该来源自己决定怎么解读——可能是全量、也可能是自己的默认起点）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT since_value FROM ingest_cursor WHERE source = ?", (source,)
            ).fetchone()
            return row["since_value"] if row else None

    def set_cursor(self, source: str, since_value: str) -> None:
        with self._lock:
            # 用 INSERT OR REPLACE 而不是更新版 SQL 的 ON CONFLICT ... DO UPDATE，
            # 前者从 SQLite 最早期版本就支持，兼容性更好——部署服务器的系统 SQLite
            # 版本未知，不假设它足够新。
            self._conn.execute(
                "INSERT OR REPLACE INTO ingest_cursor (source, since_value) VALUES (?, ?)",
                (source, since_value),
            )
            self._conn.commit()


def _row_to_problem(row: sqlite3.Row) -> StoredProblem:
    embedding_blob = row["embedding"]
    embedding = unpack_embedding(embedding_blob) if embedding_blob is not None else None
    return StoredProblem(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        url=row["url"],
        statement=row["statement"],
        embedding=embedding,
        content_hash=row["content_hash"],
        created_at=row["created_at"],
    )
