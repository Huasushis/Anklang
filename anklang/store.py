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
  source_updated_at 来源给出的题目更新时间；用于拒绝旧版本覆盖新版本，可为空
  created_at      入库时间（UTC ISO 8601 字符串，带毫秒和 Z 后缀）
  updated_at      最近一次实际更新的时间；完全相同的重复输入不会刷新

(source, external_id) 上有唯一约束，保证同一来源的同一道题不会重复入库；再次抓到
同一编号时会比较内容和来源更新时间：只有明确较新的版本才更新；时间缺失、旧版本
或同一时间的冲突内容都会保留原记录。

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
from typing import Any, Literal

from .sources import is_valid_source_updated_at
from .vectormath import pack_embedding, unpack_embedding

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS problems (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT,
        statement TEXT NOT NULL,
        embedding BLOB,
        content_hash TEXT NOT NULL,
        source_updated_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(source, external_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_cursor (
        source TEXT PRIMARY KEY,
        since_value TEXT NOT NULL
    )
    """,
)


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
    source_updated_at: str | None
    created_at: str
    updated_at: str


ProblemWriteResult = Literal["inserted", "updated", "unchanged", "skipped"]


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class ProblemStore:
    """本地题库的 SQLite 存取封装。"""

    def __init__(self, db_path: str, *, now: Any = _utc_now_iso) -> None:
        self._db_path = db_path
        self._now = now
        self._lock = threading.Lock()
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        # check_same_thread=False：ThreadingHTTPServer 每个请求一个线程、后台 ingest
        # 又是另一个线程，都要共用这一个连接；线程安全完全靠 self._lock 保证，不依赖
        # sqlite3 自己的线程检查。
        connection = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            with self._lock:
                try:
                    # 先取得数据库写锁，再检查旧库缺少哪些列。另一个进程只能在
                    # 本次迁移提交后重新读取列，不能同时尝试添加同名列。
                    connection.execute("BEGIN IMMEDIATE")
                    for statement in _SCHEMA_STATEMENTS:
                        connection.execute(statement)
                    columns = {
                        str(row["name"])
                        for row in connection.execute(
                            "PRAGMA table_info(problems)"
                        ).fetchall()
                    }
                    if "updated_at" not in columns:
                        # 兼容阶段 2 初版创建的数据库：保留首次入库时间，并用它
                        # 初始化新增的最近更新时间。
                        connection.execute(
                            "ALTER TABLE problems ADD COLUMN updated_at TEXT"
                        )
                    # 如果上一次迁移在新增列之后、补值之前被中断，本次启动仍会
                    # 修复空值。迁移只修改结构，不读取或输出题面。
                    connection.execute(
                        "UPDATE problems SET updated_at = created_at "
                        "WHERE updated_at IS NULL"
                    )
                    if "source_updated_at" not in columns:
                        connection.execute(
                            "ALTER TABLE problems ADD COLUMN source_updated_at TEXT"
                        )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except BaseException:
            connection.close()
            raise
        self._conn = connection

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
        source_updated_at: str | None = None,
    ) -> ProblemWriteResult:
        """新增或更新一道题，返回 inserted / updated / unchanged / skipped。

        同一来源、同一编号的题面发生变化时必须清掉旧向量；否则旧题面的向量会被
        当成新题面使用。只有标题或链接变化时保留现有向量。完全相同的重复输入不
        执行 UPDATE，也不刷新 updated_at。来源没有稳定更新时间，或传入版本不比
        已存版本新时，不猜测到达顺序，返回 skipped 并保留原记录。
        """
        if source_updated_at is not None and not is_valid_source_updated_at(
            source_updated_at
        ):
            raise ValueError("来源更新时间必须是规范的 UTC 时间。")
        blob = pack_embedding(embedding) if embedding is not None else None
        with self._lock:
            # 先取得数据库写锁，再读取和决定新增/更新。这样另一个进程不能在
            # SELECT 与 INSERT 之间插入同一来源、同一编号的记录。
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    """
                    SELECT id, title, url, statement, embedding, content_hash,
                           source_updated_at
                    FROM problems WHERE source = ? AND external_id = ?
                    """,
                    (source, external_id),
                ).fetchone()
                now = self._now()
                if existing is None:
                    self._conn.execute(
                        """
                        INSERT INTO problems
                            (source, external_id, title, url, statement, embedding,
                             content_hash, source_updated_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source,
                            external_id,
                            title,
                            url,
                            statement,
                            blob,
                            content_hash,
                            source_updated_at,
                            now,
                            now,
                        ),
                    )
                    self._conn.commit()
                    return "inserted"

                content_changed = (
                    existing["statement"] != statement
                    or existing["content_hash"] != content_hash
                )
                source_fields_changed = (
                    existing["title"] != title
                    or existing["url"] != url
                    or content_changed
                )
                existing_source_updated_at = existing["source_updated_at"]
                if source_fields_changed and (
                    source_updated_at is None
                    or existing_source_updated_at is None
                    or source_updated_at <= existing_source_updated_at
                ):
                    self._conn.commit()
                    return "skipped"

                if content_changed:
                    next_embedding = blob
                elif existing["embedding"] is None and blob is not None:
                    next_embedding = blob
                else:
                    next_embedding = existing["embedding"]

                next_source_updated_at = existing_source_updated_at
                if source_updated_at is not None and (
                    existing_source_updated_at is None
                    or source_updated_at > existing_source_updated_at
                ):
                    next_source_updated_at = source_updated_at

                changed = (
                    source_fields_changed
                    or existing["embedding"] != next_embedding
                    or existing_source_updated_at != next_source_updated_at
                )
                if not changed:
                    self._conn.commit()
                    return "unchanged"

                self._conn.execute(
                    """
                    UPDATE problems
                    SET title = ?, url = ?, statement = ?, embedding = ?,
                        content_hash = ?, source_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        url,
                        statement,
                        next_embedding,
                        content_hash,
                        next_source_updated_at,
                        now,
                        existing["id"],
                    ),
                )
                self._conn.commit()
                return "updated"
            except BaseException:
                self._conn.rollback()
                raise

    def add_problems_batch(self, problems: Iterable[dict[str, Any]]) -> int:
        """批量新增或更新，返回真正发生写入的条数。"""
        written = 0
        for problem in problems:
            if self.add_problem(**problem) in {"inserted", "updated"}:
                written += 1
        return written

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM problems").fetchone()
            return int(row["c"])

    def get_problem(self, source: str, external_id: str) -> StoredProblem | None:
        """按来源和题号读取一条记录；导入前用它判断是否真的需要计算向量。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, source, external_id, title, url, statement, embedding, "
                "content_hash, source_updated_at, created_at, updated_at "
                "FROM problems WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
        return _row_to_problem(row) if row is not None else None

    def iter_all(self) -> list[StoredProblem]:
        """取出全部题目（本地引擎检索时逐条计算相似度用）。一次性取成列表而不是
        生成器，避免在持锁期间把锁的释放时机和调用方遍历的节奏绑在一起。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source, external_id, title, url, statement, embedding, "
                "content_hash, source_updated_at, created_at, updated_at FROM problems"
            ).fetchall()
        return [_row_to_problem(row) for row in rows]

    def iter_missing_embeddings(self) -> list[StoredProblem]:
        """取出还没算出向量的题目（embedding backfill 用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source, external_id, title, url, statement, embedding, "
                "content_hash, source_updated_at, created_at, updated_at "
                "FROM problems WHERE embedding IS NULL"
            ).fetchall()
        return [_row_to_problem(row) for row in rows]

    def update_embedding(
        self,
        problem_id: int,
        embedding: list[float],
        *,
        expected_content_hash: str | None = None,
    ) -> bool:
        """写回补算向量。若题面摘要已经变化则拒绝旧结果，返回 False。"""
        blob = pack_embedding(embedding)
        with self._lock:
            if expected_content_hash is None:
                cursor = self._conn.execute(
                    "UPDATE problems SET embedding = ?, updated_at = ? "
                    "WHERE id = ? AND embedding IS NULL",
                    (blob, self._now(), problem_id),
                )
            else:
                cursor = self._conn.execute(
                    """
                    UPDATE problems SET embedding = ?, updated_at = ?
                    WHERE id = ? AND content_hash = ? AND embedding IS NULL
                    """,
                    (blob, self._now(), problem_id, expected_content_hash),
                )
            self._conn.commit()
            return cursor.rowcount > 0

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

    def set_cursor_if_current(
        self,
        source: str,
        expected_value: str | None,
        next_value: str,
    ) -> bool:
        """只在游标仍等于本轮开始值时推进；并发旧任务不能覆盖较新的游标。"""
        if not is_valid_source_updated_at(next_value):
            raise ValueError("下一游标必须是规范的 UTC 时间。")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT since_value FROM ingest_cursor WHERE source = ?",
                    (source,),
                ).fetchone()
                current = row["since_value"] if row is not None else None
                if current != expected_value or (
                    current is not None
                    and is_valid_source_updated_at(current)
                    and next_value <= current
                ):
                    self._conn.commit()
                    return False
                if current is None:
                    self._conn.execute(
                        "INSERT INTO ingest_cursor (source, since_value) VALUES (?, ?)",
                        (source, next_value),
                    )
                else:
                    self._conn.execute(
                        "UPDATE ingest_cursor SET since_value = ? "
                        "WHERE source = ? AND since_value = ?",
                        (next_value, source, current),
                    )
                self._conn.commit()
                return True
            except BaseException:
                self._conn.rollback()
                raise


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
        source_updated_at=row["source_updated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
