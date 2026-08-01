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

index_metadata 表由程序生成，严格绑定向量模型、维度、构建版本、实际语料修订值与
行数。它不是可随意填写的配置表；缺失、冲突或旧库有身份未知的向量时，向量读写会
关闭，但题目和旧向量都会原样保留，供运维另建数据库重建和回滚。

并发说明：整个 ProblemStore 只持有一个 sqlite3 连接（check_same_thread=False），
所有方法都在同一把进程内锁（self._lock）保护下执行，不追求高并发读写性能——本地
引擎目前是面向"未来自建题库"的框架性实现，量级和并发都远低于需要精细优化的程度；
需要更高并发时再按 docs/plan.md 3.2 节的预案升级到 Postgres。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .sources import is_valid_source_updated_at
from .vectormath import pack_embedding, unpack_embedding, validate_embedding

INDEX_METADATA_SCHEMA_VERSION = "1"
INDEX_BUILD_REVISION = "anklang-direct-embedding-xor-v1"
NO_EMBEDDING_MODEL = "anklang-keyword-only"
_CORPUS_REVISION_DOMAIN = b"anklang-corpus-revision-v1\x00"
_CACHE_IDENTITY_DOMAIN = b"anklang-local-cache-identity-v1\x00"
_INDEX_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "embedding_model",
        "embedding_dimensions",
        "corpus_revision",
        "index_build_revision",
        "problem_count",
        "embedding_rows",
    }
)

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
    """
    CREATE TABLE IF NOT EXISTS index_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
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


@dataclass(frozen=True)
class EmbeddingIndexSpec:
    """一批向量允许共存所必须完全一致的机器规格。"""

    model: str
    dimensions: int
    build_revision: str = INDEX_BUILD_REVISION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model, str)
            or not self.model
            or self.model == NO_EMBEDDING_MODEL
            or len(self.model) > 200
            or any(ord(character) < 32 for character in self.model)
        ):
            raise ValueError("向量模型标识不合法。")
        if (
            isinstance(self.dimensions, bool)
            or not isinstance(self.dimensions, int)
            or not 1 <= self.dimensions <= 4096
        ):
            raise ValueError("向量维度不合法。")
        if self.build_revision != INDEX_BUILD_REVISION:
            raise ValueError("索引构建版本不受支持。")


@dataclass(frozen=True)
class IndexMetadata:
    schema_version: str
    embedding_model: str
    embedding_dimensions: int
    corpus_revision: str
    index_build_revision: str
    problem_count: int
    embedding_rows: int

    def as_rows(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": str(self.embedding_dimensions),
            "corpus_revision": self.corpus_revision,
            "index_build_revision": self.index_build_revision,
            "problem_count": str(self.problem_count),
            "embedding_rows": str(self.embedding_rows),
        }


@dataclass(frozen=True)
class SearchSnapshot:
    problems: tuple[StoredProblem, ...]
    vector_ready: bool
    vector_status: str
    cache_identity: str | None = None


@dataclass(frozen=True)
class IndexInspection:
    problem_count: int | None
    metadata_ready: bool
    vector_ready: bool
    status: str
    cache_identity: str | None = None


@dataclass(frozen=True)
class _ActualIndexState:
    problem_count: int
    embedding_rows: int
    corpus_revision: str


class IndexMetadataError(RuntimeError):
    """索引身份不可信；消息和状态均不得包含配置值或题面。"""

    def __init__(self, status: str = "invalid") -> None:
        self.status = status
        super().__init__("本地向量索引元数据不完整或与当前配置不一致。")


ProblemWriteResult = Literal["inserted", "updated", "unchanged", "skipped"]


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def calculate_corpus_revision(
    identities: Iterable[tuple[str, str, str]],
) -> str:
    """由实际题目身份和题面摘要集合生成稳定、与行顺序无关的语料修订值。"""

    revision, _ = _calculate_corpus_state(identities)
    return revision


def _calculate_corpus_state(
    identities: Iterable[tuple[str, str, str]],
) -> tuple[str, int]:
    """同时计算集合承诺和题目数，供流式 preflight 避免保留第二份行列表。"""

    aggregate = bytearray(32)
    seen: set[tuple[str, str]] = set()
    count = 0
    for source, external_id, content_hash in identities:
        identity = (source, external_id)
        if identity in seen:
            raise ValueError("语料包含重复题目身份。")
        seen.add(identity)
        count += 1
        _xor_digest(aggregate, _corpus_row_digest(source, external_id, content_hash))
    return bytes(aggregate).hex(), count


def _corpus_row_digest(source: str, external_id: str, content_hash: str) -> bytes:
    digest = hashlib.sha256(_CORPUS_REVISION_DOMAIN)
    for value in (source, external_id, content_hash):
        raw = value.encode("utf-8")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.digest()


def _xor_digest(target: bytearray, value: bytes) -> None:
    if len(target) != 32 or len(value) != 32:
        raise ValueError("语料修订值长度不合法。")
    for index, byte in enumerate(value):
        target[index] ^= byte


def _toggle_corpus_revision(
    revision: str,
    *identities: tuple[str, str, str],
) -> str:
    try:
        aggregate = bytearray.fromhex(revision)
    except ValueError as error:
        raise IndexMetadataError("metadata_shape") from error
    if len(aggregate) != 32:
        raise IndexMetadataError("metadata_shape")
    for identity in identities:
        _xor_digest(aggregate, _corpus_row_digest(*identity))
    return bytes(aggregate).hex()


def parse_index_metadata(values: dict[str, str]) -> IndexMetadata:
    """严格解析正式元数据；未知键也视为不兼容，必须显式升级。"""

    if set(values) != _INDEX_METADATA_KEYS:
        raise IndexMetadataError("metadata_shape")
    try:
        dimensions = _parse_metadata_count(values["embedding_dimensions"])
        problem_count = _parse_metadata_count(values["problem_count"])
        embedding_rows = _parse_metadata_count(values["embedding_rows"])
    except (KeyError, ValueError) as error:
        raise IndexMetadataError("metadata_shape") from error
    model = values["embedding_model"]
    corpus_revision = values["corpus_revision"]
    build_revision = values["index_build_revision"]
    if (
        values["schema_version"] != INDEX_METADATA_SCHEMA_VERSION
        or len(model) > 200
        or any(ord(character) < 32 for character in model)
        or len(corpus_revision) != 64
        or any(character not in "0123456789abcdef" for character in corpus_revision)
        or not build_revision
        or len(build_revision) > 200
        or embedding_rows > problem_count
        or (
            model == NO_EMBEDDING_MODEL
            and (dimensions != 0 or embedding_rows != 0)
        )
        or (model != NO_EMBEDDING_MODEL and (not model or dimensions == 0))
    ):
        raise IndexMetadataError("metadata_shape")
    return IndexMetadata(
        schema_version=values["schema_version"],
        embedding_model=model,
        embedding_dimensions=dimensions,
        corpus_revision=corpus_revision,
        index_build_revision=build_revision,
        problem_count=problem_count,
        embedding_rows=embedding_rows,
    )


def _parse_metadata_count(value: str, *, positive: bool = False) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid metadata integer")
    number = int(value)
    if str(number) != value or number < (1 if positive else 0):
        raise ValueError("invalid metadata integer")
    return number


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
        self._verified_data_version: int | None = None
        self._verified_mode: tuple[str, int, str] | None = None
        self._verified_status = "verification_required"
        self._verified_metadata: IndexMetadata | None = None
        self._verified_problem_count: int | None = None
        self._write_prepared_mode: tuple[str, int, str] | None = None
        # SQLite data_version 不会因同一连接自己的提交而变化。缓存身份另带一份
        # 进程内写入代际，使标题、链接或其他不改变 corpus_revision 的正式更新
        # 也不能复用提交前的候选或模型判断；进程重启时内存缓存本来也会清空。
        self._cache_generation = 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def prepare_keyword_writes(self) -> IndexMetadata:
        """把无向量题库登记为正式关键词索引；已有未知向量时仍拒绝认领。"""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                observed_data_version = self._data_version_locked()
                values = self._metadata_values_locked()
                if values is None:
                    state = self._actual_index_state_locked(
                        expected_dimensions=None,
                        validate_vectors=False,
                    )
                    if state.embedding_rows > 0:
                        raise IndexMetadataError("legacy_vectors")
                    metadata = IndexMetadata(
                        schema_version=INDEX_METADATA_SCHEMA_VERSION,
                        embedding_model=NO_EMBEDDING_MODEL,
                        embedding_dimensions=0,
                        corpus_revision=state.corpus_revision,
                        index_build_revision=INDEX_BUILD_REVISION,
                        problem_count=state.problem_count,
                        embedding_rows=0,
                    )
                    self._conn.executemany(
                        "INSERT INTO index_metadata (key, value) VALUES (?, ?)",
                        metadata.as_rows().items(),
                    )
                else:
                    metadata = parse_index_metadata(values)
                    state = self._actual_index_state_locked(
                        expected_dimensions=(
                            metadata.embedding_dimensions
                            if metadata.embedding_model != NO_EMBEDDING_MODEL
                            else None
                        ),
                        validate_vectors=(
                            metadata.embedding_model != NO_EMBEDDING_MODEL
                        ),
                    )
                    self._validate_keyword_metadata_locked(metadata, state)
                self._conn.commit()
                self._record_verification_locked(
                    None,
                    _complete_status(metadata, None),
                    metadata,
                    data_version=observed_data_version,
                )
                self._write_prepared_mode = _metadata_mode(metadata)
                return metadata
            except BaseException:
                self._conn.rollback()
                raise

    def prepare_embedding_writes(self, spec: EmbeddingIndexSpec) -> IndexMetadata:
        """在任何模型调用前锁定向量规格；绝不自动认领已有的未知向量。"""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                observed_data_version = self._data_version_locked()
                values = self._metadata_values_locked()
                if values is None:
                    state = self._actual_index_state_locked(
                        expected_dimensions=None,
                        validate_vectors=False,
                    )
                    if state.embedding_rows > 0:
                        raise IndexMetadataError("legacy_vectors")
                    metadata = IndexMetadata(
                        schema_version=INDEX_METADATA_SCHEMA_VERSION,
                        embedding_model=spec.model,
                        embedding_dimensions=spec.dimensions,
                        corpus_revision=state.corpus_revision,
                        index_build_revision=spec.build_revision,
                        problem_count=state.problem_count,
                        embedding_rows=0,
                    )
                    self._conn.executemany(
                        "INSERT INTO index_metadata (key, value) VALUES (?, ?)",
                        metadata.as_rows().items(),
                    )
                else:
                    metadata = parse_index_metadata(values)
                    if metadata.embedding_model == NO_EMBEDDING_MODEL:
                        state = self._actual_index_state_locked(
                            expected_dimensions=None,
                            validate_vectors=False,
                        )
                        self._validate_keyword_metadata_locked(metadata, state)
                        replacements = {
                            "embedding_model": spec.model,
                            "embedding_dimensions": str(spec.dimensions),
                        }
                        for key, value in replacements.items():
                            cursor = self._conn.execute(
                                "UPDATE index_metadata SET value = ? WHERE key = ?",
                                (value, key),
                            )
                            if cursor.rowcount != 1:
                                raise IndexMetadataError("metadata_shape")
                        metadata = IndexMetadata(
                            schema_version=metadata.schema_version,
                            embedding_model=spec.model,
                            embedding_dimensions=spec.dimensions,
                            corpus_revision=metadata.corpus_revision,
                            index_build_revision=metadata.index_build_revision,
                            problem_count=metadata.problem_count,
                            embedding_rows=metadata.embedding_rows,
                        )
                    else:
                        if metadata.embedding_model != spec.model:
                            raise IndexMetadataError("model_mismatch")
                        if metadata.embedding_dimensions != spec.dimensions:
                            raise IndexMetadataError("dimension_mismatch")
                        if metadata.index_build_revision != spec.build_revision:
                            raise IndexMetadataError("build_mismatch")
                        state = self._actual_index_state_locked(
                            expected_dimensions=spec.dimensions,
                            validate_vectors=True,
                        )
                    self._validate_metadata_locked(metadata, spec, state)
                self._conn.commit()
                self._record_verification_locked(
                    spec,
                    _complete_status(metadata, spec),
                    metadata,
                    data_version=observed_data_version,
                )
                self._write_prepared_mode = _metadata_mode(metadata)
                return metadata
            except BaseException:
                self._conn.rollback()
                raise

    def get_index_metadata(self) -> IndexMetadata | None:
        """读取正式元数据；仅供运维验证和测试，不会尝试修复。"""

        with self._lock:
            values = self._metadata_values_locked()
            return parse_index_metadata(values) if values is not None else None

    def search_snapshot(self, spec: EmbeddingIndexSpec | None) -> SearchSnapshot:
        """读取一次一致快照；向量身份不可信时保留关键词数据但不返回任何向量。"""

        with self._lock:
            self._conn.execute("BEGIN")
            try:
                observed_data_version = self._data_version_locked()
                rows = self._problem_rows_locked()
                keyword_problems = tuple(
                    _row_to_problem(row, include_embedding=False) for row in rows
                )
                try:
                    values = self._metadata_values_locked()
                except IndexMetadataError as error:
                    self._conn.commit()
                    self._record_verification_locked(
                        spec,
                        error.status,
                        None,
                        data_version=observed_data_version,
                        problem_count=len(rows),
                    )
                    return SearchSnapshot(
                        keyword_problems,
                        False,
                        error.status,
                    )
                if spec is None:
                    metadata: IndexMetadata | None = None
                    if values is None:
                        status = (
                            "legacy_vectors"
                            if any(row["embedding"] is not None for row in rows)
                            else "uninitialized"
                        )
                    else:
                        try:
                            metadata = parse_index_metadata(values)
                            state = _actual_index_state_from_rows(
                                rows,
                                expected_dimensions=(
                                    metadata.embedding_dimensions
                                    if metadata.embedding_model
                                    != NO_EMBEDDING_MODEL
                                    else None
                                ),
                                validate_vectors=(
                                    metadata.embedding_model
                                    != NO_EMBEDDING_MODEL
                                ),
                            )
                            self._validate_keyword_metadata_locked(metadata, state)
                            status = _complete_status(metadata, None)
                        except (IndexMetadataError, ValueError) as error:
                            status = (
                                error.status
                                if isinstance(error, IndexMetadataError)
                                else "invalid_vectors"
                            )
                            metadata = None
                    self._conn.commit()
                    self._record_verification_locked(
                        spec,
                        status,
                        metadata,
                        data_version=observed_data_version,
                        problem_count=len(rows),
                    )
                    return SearchSnapshot(
                        keyword_problems,
                        False,
                        status,
                        _index_cache_identity(
                            metadata,
                            spec,
                            status,
                            data_version=observed_data_version,
                            cache_generation=self._cache_generation,
                        ),
                    )
                if values is None:
                    status = (
                        "legacy_vectors"
                        if any(row["embedding"] is not None for row in rows)
                        else "uninitialized"
                    )
                    self._conn.commit()
                    self._record_verification_locked(
                        spec,
                        status,
                        None,
                        data_version=observed_data_version,
                        problem_count=len(rows),
                    )
                    return SearchSnapshot(keyword_problems, False, status)
                metadata = None
                try:
                    metadata = parse_index_metadata(values)
                    if metadata.embedding_model != spec.model:
                        raise IndexMetadataError("model_mismatch")
                    if metadata.embedding_dimensions != spec.dimensions:
                        raise IndexMetadataError("dimension_mismatch")
                    if metadata.index_build_revision != spec.build_revision:
                        raise IndexMetadataError("build_mismatch")
                    state = _actual_index_state_from_rows(
                        rows,
                        expected_dimensions=spec.dimensions,
                        validate_vectors=True,
                    )
                    self._validate_metadata_locked(metadata, spec, state)
                    status = _complete_status(metadata, spec)
                    if status != "ready":
                        self._conn.commit()
                        self._record_verification_locked(
                            spec,
                            status,
                            metadata,
                            data_version=observed_data_version,
                        )
                        return SearchSnapshot(
                            keyword_problems,
                            False,
                            status,
                        )
                    vector_problems = tuple(
                        _row_to_problem(
                            row,
                            include_embedding=True,
                            expected_dimensions=spec.dimensions,
                        )
                        for row in rows
                    )
                except (IndexMetadataError, ValueError) as error:
                    status = (
                        error.status
                        if isinstance(error, IndexMetadataError)
                        else "invalid_vectors"
                    )
                    metadata = None
                    self._conn.commit()
                    self._record_verification_locked(
                        spec,
                        status,
                        metadata,
                        data_version=observed_data_version,
                        problem_count=len(rows),
                    )
                    return SearchSnapshot(keyword_problems, False, status)
                self._conn.commit()
                assert metadata is not None
                self._record_verification_locked(
                    spec,
                    "ready",
                    metadata,
                    data_version=observed_data_version,
                )
                return SearchSnapshot(
                    vector_problems,
                    True,
                    "ready",
                    _index_cache_identity(
                        metadata,
                        spec,
                        "ready",
                        data_version=observed_data_version,
                        cache_generation=self._cache_generation,
                    ),
                )
            except BaseException:
                self._conn.rollback()
                raise

    def _problem_rows_locked(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, source, external_id, title, url, statement, embedding, "
            "content_hash, source_updated_at, created_at, updated_at FROM problems"
        ).fetchall()

    def _actual_index_state_locked(
        self,
        *,
        expected_dimensions: int | None,
        validate_vectors: bool,
    ) -> _ActualIndexState:
        # revision 扫描严格只取身份和摘要；向量另用流式游标验证，绝不把题面、
        # 标题或整库 BLOB 同时装进内存。
        try:
            corpus_revision, problem_count = _calculate_corpus_state(
                (
                    str(row["source"]),
                    str(row["external_id"]),
                    str(row["content_hash"]),
                )
                for row in self._conn.execute(
                    "SELECT source, external_id, content_hash FROM problems"
                )
            )
            if validate_vectors:
                embedding_rows = 0
                for row in self._conn.execute(
                    "SELECT embedding FROM problems WHERE embedding IS NOT NULL"
                ):
                    embedding_rows += 1
                    validate_embedding(
                        unpack_embedding(row["embedding"]),
                        expected_dimensions=expected_dimensions,
                    )
            else:
                row = self._conn.execute(
                    "SELECT COUNT(embedding) AS count FROM problems"
                ).fetchone()
                embedding_rows = int(row["count"])
        except ValueError as error:
            raise IndexMetadataError("invalid_vectors") from error
        return _ActualIndexState(
            problem_count=problem_count,
            embedding_rows=embedding_rows,
            corpus_revision=corpus_revision,
        )

    def _metadata_values_locked(self) -> dict[str, str] | None:
        columns = self._conn.execute(
            "PRAGMA table_info(index_metadata)"
        ).fetchall()
        observed_schema = [
            (
                str(row["name"]),
                str(row["type"]).upper(),
                int(row["notnull"]),
                int(row["pk"]),
            )
            for row in columns
        ]
        if observed_schema != [
            ("key", "TEXT", 0, 1),
            ("value", "TEXT", 1, 0),
        ]:
            raise IndexMetadataError("metadata_shape")
        rows = self._conn.execute(
            "SELECT key, value FROM index_metadata"
        ).fetchall()
        if not rows:
            return None
        values = {str(row["key"]): str(row["value"]) for row in rows}
        if len(values) != len(rows):
            raise IndexMetadataError("metadata_shape")
        return values

    def _validate_metadata_locked(
        self,
        metadata: IndexMetadata,
        spec: EmbeddingIndexSpec,
        state: _ActualIndexState,
    ) -> None:
        if metadata.embedding_model != spec.model:
            raise IndexMetadataError("model_mismatch")
        if metadata.embedding_dimensions != spec.dimensions:
            raise IndexMetadataError("dimension_mismatch")
        if metadata.index_build_revision != spec.build_revision:
            raise IndexMetadataError("build_mismatch")
        self._validate_dynamic_metadata_locked(metadata, state)

    def _validate_keyword_metadata_locked(
        self,
        metadata: IndexMetadata,
        state: _ActualIndexState,
    ) -> None:
        if metadata.index_build_revision != INDEX_BUILD_REVISION:
            raise IndexMetadataError("build_mismatch")
        self._validate_dynamic_metadata_locked(metadata, state)
        if metadata.embedding_model == NO_EMBEDDING_MODEL:
            if metadata.embedding_dimensions != 0 or metadata.embedding_rows != 0:
                raise IndexMetadataError("metadata_shape")
            return
        try:
            spec = EmbeddingIndexSpec(
                model=metadata.embedding_model,
                dimensions=metadata.embedding_dimensions,
            )
        except ValueError as error:
            raise IndexMetadataError("metadata_shape") from error
        self._validate_metadata_locked(metadata, spec, state)

    def _validate_dynamic_metadata_locked(
        self,
        metadata: IndexMetadata,
        state: _ActualIndexState,
    ) -> None:
        if (
            metadata.problem_count != state.problem_count
            or metadata.embedding_rows != state.embedding_rows
            or metadata.corpus_revision != state.corpus_revision
        ):
            raise IndexMetadataError("metadata_stale")

    def _advance_metadata_locked(
        self,
        metadata: IndexMetadata,
        *,
        problem_delta: int = 0,
        embedding_delta: int = 0,
        toggle_identities: tuple[tuple[str, str, str], ...] = (),
    ) -> IndexMetadata:
        problem_count = metadata.problem_count + problem_delta
        embedding_rows = metadata.embedding_rows + embedding_delta
        if problem_count < 0 or not 0 <= embedding_rows <= problem_count:
            raise IndexMetadataError("metadata_stale")
        corpus_revision = _toggle_corpus_revision(
            metadata.corpus_revision,
            *toggle_identities,
        )
        next_metadata = IndexMetadata(
            schema_version=metadata.schema_version,
            embedding_model=metadata.embedding_model,
            embedding_dimensions=metadata.embedding_dimensions,
            corpus_revision=corpus_revision,
            index_build_revision=metadata.index_build_revision,
            problem_count=problem_count,
            embedding_rows=embedding_rows,
        )
        updates: dict[str, str] = {}
        if problem_delta:
            updates["problem_count"] = str(problem_count)
        if embedding_delta:
            updates["embedding_rows"] = str(embedding_rows)
        if toggle_identities:
            updates["corpus_revision"] = corpus_revision
        for key, value in updates.items():
            cursor = self._conn.execute(
                "UPDATE index_metadata SET value = ? WHERE key = ?",
                (value, key),
            )
            if cursor.rowcount != 1:
                raise IndexMetadataError("metadata_shape")
        return next_metadata

    def _metadata_before_write_locked(
        self,
        *,
        index_spec: EmbeddingIndexSpec | None,
        vector_write: bool,
    ) -> IndexMetadata | None:
        values = self._metadata_values_locked()
        if values is None:
            if vector_write:
                raise IndexMetadataError("uninitialized")
            return None
        metadata = parse_index_metadata(values)
        if self._verified_data_version != self._data_version_locked():
            raise IndexMetadataError("verification_required")
        if metadata.index_build_revision != INDEX_BUILD_REVISION:
            raise IndexMetadataError("build_mismatch")
        if metadata.embedding_model == NO_EMBEDDING_MODEL:
            if vector_write or index_spec is not None:
                raise IndexMetadataError("uninitialized")
            if self._write_prepared_mode != _metadata_mode(metadata):
                raise IndexMetadataError("preflight_required")
            return metadata
        try:
            stored_spec = EmbeddingIndexSpec(
                model=metadata.embedding_model,
                dimensions=metadata.embedding_dimensions,
            )
        except ValueError as error:
            raise IndexMetadataError("metadata_shape") from error
        requested_spec = index_spec if index_spec is not None else stored_spec
        if metadata.embedding_model != requested_spec.model:
            raise IndexMetadataError("model_mismatch")
        if metadata.embedding_dimensions != requested_spec.dimensions:
            raise IndexMetadataError("dimension_mismatch")
        if metadata.index_build_revision != requested_spec.build_revision:
            raise IndexMetadataError("build_mismatch")
        if self._write_prepared_mode != _metadata_mode(metadata):
            raise IndexMetadataError("preflight_required")
        return metadata

    def inspect_index(
        self,
        spec: EmbeddingIndexSpec | None,
    ) -> IndexInspection:
        """健康检查专用：只读 data_version 与七行元数据，不扫描题面或向量。"""

        with self._lock:
            data_version = self._data_version_locked()
            if (
                self._verified_data_version != data_version
                or self._verified_mode != _mode_key(spec)
            ):
                return IndexInspection(
                    problem_count=None,
                    metadata_ready=False,
                    vector_ready=False,
                    status="verification_required",
                )
            if self._verified_metadata is None:
                return IndexInspection(
                    problem_count=self._verified_problem_count,
                    metadata_ready=False,
                    vector_ready=False,
                    status=self._verified_status,
                )
            try:
                values = self._metadata_values_locked()
                metadata = (
                    parse_index_metadata(values) if values is not None else None
                )
            except IndexMetadataError as error:
                return IndexInspection(
                    problem_count=None,
                    metadata_ready=False,
                    vector_ready=False,
                    status=error.status,
                )
            if metadata != self._verified_metadata:
                return IndexInspection(
                    problem_count=None,
                    metadata_ready=False,
                    vector_ready=False,
                    status="verification_required",
                )
            return IndexInspection(
                problem_count=metadata.problem_count,
                metadata_ready=self._verified_status in {"ready", "disabled"},
                vector_ready=self._verified_status == "ready",
                status=self._verified_status,
                cache_identity=_index_cache_identity(
                    metadata,
                    spec,
                    self._verified_status,
                    data_version=data_version,
                    cache_generation=self._cache_generation,
                ),
            )

    def _data_version_locked(self) -> int:
        row = self._conn.execute("PRAGMA data_version").fetchone()
        return int(row[0])

    def _record_verification_locked(
        self,
        spec: EmbeddingIndexSpec | None,
        status: str,
        metadata: IndexMetadata | None,
        *,
        data_version: int,
        problem_count: int | None = None,
    ) -> None:
        self._verified_data_version = data_version
        self._verified_mode = _mode_key(spec)
        self._verified_status = status
        self._verified_metadata = metadata
        self._verified_problem_count = (
            metadata.problem_count if metadata is not None else problem_count
        )

    def _record_own_write_locked(self, metadata: IndexMetadata) -> None:
        if self._verified_data_version is None:
            raise IndexMetadataError("preflight_required")
        if self._verified_mode == _mode_key(None):
            spec = None
        elif self._verified_mode is not None:
            spec = EmbeddingIndexSpec(
                model=self._verified_mode[0],
                dimensions=self._verified_mode[1],
            )
        else:
            raise IndexMetadataError("preflight_required")
        self._cache_generation += 1
        self._record_verification_locked(
            spec,
            _complete_status(metadata, spec),
            metadata,
            data_version=self._verified_data_version,
        )

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
        index_spec: EmbeddingIndexSpec | None = None,
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
        if embedding is not None and index_spec is None:
            raise IndexMetadataError("missing_write_spec")
        if embedding is not None and index_spec is not None:
            validate_embedding(embedding, expected_dimensions=index_spec.dimensions)
        blob = pack_embedding(embedding) if embedding is not None else None
        with self._lock:
            # 先取得数据库写锁，再读取和决定新增/更新。这样另一个进程不能在
            # SELECT 与 INSERT 之间插入同一来源、同一编号的记录。
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._metadata_before_write_locked(
                    index_spec=index_spec,
                    vector_write=embedding is not None,
                )
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
                    if metadata is not None:
                        metadata = self._advance_metadata_locked(
                            metadata,
                            problem_delta=1,
                            embedding_delta=1 if blob is not None else 0,
                            toggle_identities=(
                                (source, external_id, content_hash),
                            ),
                        )
                    self._conn.commit()
                    if metadata is not None:
                        self._record_own_write_locked(metadata)
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
                if metadata is not None:
                    toggle_identities: tuple[tuple[str, str, str], ...] = ()
                    if content_changed:
                        toggle_identities = (
                            (source, external_id, str(existing["content_hash"])),
                            (source, external_id, content_hash),
                        )
                    metadata = self._advance_metadata_locked(
                        metadata,
                        embedding_delta=(
                            int(next_embedding is not None)
                            - int(existing["embedding"] is not None)
                        ),
                        toggle_identities=toggle_identities,
                    )
                self._conn.commit()
                if metadata is not None:
                    self._record_own_write_locked(metadata)
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
        index_spec: EmbeddingIndexSpec,
        expected_content_hash: str | None = None,
    ) -> bool:
        """写回补算向量。若题面摘要已经变化则拒绝旧结果，返回 False。"""
        validate_embedding(embedding, expected_dimensions=index_spec.dimensions)
        blob = pack_embedding(embedding)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._metadata_before_write_locked(
                    index_spec=index_spec,
                    vector_write=True,
                )
                assert metadata is not None
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
                if cursor.rowcount > 0:
                    metadata = self._advance_metadata_locked(
                        metadata,
                        embedding_delta=1,
                    )
                self._conn.commit()
                if cursor.rowcount > 0:
                    self._record_own_write_locked(metadata)
                return cursor.rowcount > 0
            except BaseException:
                self._conn.rollback()
                raise

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


def _mode_key(
    spec: EmbeddingIndexSpec | None,
) -> tuple[str, int, str]:
    if spec is None:
        return (NO_EMBEDDING_MODEL, 0, INDEX_BUILD_REVISION)
    return (spec.model, spec.dimensions, spec.build_revision)


def _metadata_mode(metadata: IndexMetadata) -> tuple[str, int, str]:
    return (
        metadata.embedding_model,
        metadata.embedding_dimensions,
        metadata.index_build_revision,
    )


def _complete_status(
    metadata: IndexMetadata,
    spec: EmbeddingIndexSpec | None,
) -> str:
    if metadata.problem_count == 0:
        return "empty_corpus"
    if (
        metadata.embedding_model != NO_EMBEDDING_MODEL
        and metadata.embedding_rows != metadata.problem_count
    ):
        return "incomplete_vectors"
    if spec is None:
        return "disabled"
    return "ready"


def _index_cache_identity(
    metadata: IndexMetadata | None,
    spec: EmbeddingIndexSpec | None,
    status: str,
    *,
    data_version: int,
    cache_generation: int,
) -> str | None:
    """绑定一次可缓存结果的索引身份；只返回摘要，不向健康响应暴露模型名。"""

    if metadata is None or status not in {"ready", "disabled"}:
        return None
    digest = hashlib.sha256(_CACHE_IDENTITY_DOMAIN)
    requested_mode = _mode_key(spec)
    values = (
        metadata.schema_version,
        metadata.embedding_model,
        str(metadata.embedding_dimensions),
        metadata.corpus_revision,
        metadata.index_build_revision,
        str(metadata.problem_count),
        str(metadata.embedding_rows),
        requested_mode[0],
        str(requested_mode[1]),
        requested_mode[2],
        status,
        str(data_version),
        str(cache_generation),
    )
    for value in values:
        raw = value.encode("utf-8")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _actual_index_state_from_rows(
    rows: Iterable[sqlite3.Row],
    *,
    expected_dimensions: int | None,
    validate_vectors: bool,
) -> _ActualIndexState:
    aggregate = bytearray(32)
    seen: set[tuple[str, str]] = set()
    problem_count = 0
    embedding_rows = 0
    try:
        for row in rows:
            source = str(row["source"])
            external_id = str(row["external_id"])
            content_hash = str(row["content_hash"])
            identity = (source, external_id)
            if identity in seen:
                raise ValueError("语料包含重复题目身份。")
            seen.add(identity)
            problem_count += 1
            _xor_digest(
                aggregate,
                _corpus_row_digest(source, external_id, content_hash),
            )
            blob = row["embedding"]
            if blob is None:
                continue
            embedding_rows += 1
            if validate_vectors:
                validate_embedding(
                    unpack_embedding(blob),
                    expected_dimensions=expected_dimensions,
                )
    except ValueError as error:
        raise IndexMetadataError("invalid_vectors") from error
    return _ActualIndexState(
        problem_count=problem_count,
        embedding_rows=embedding_rows,
        corpus_revision=bytes(aggregate).hex(),
    )


def _row_to_problem(
    row: sqlite3.Row,
    *,
    include_embedding: bool = True,
    expected_dimensions: int | None = None,
) -> StoredProblem:
    embedding_blob = row["embedding"]
    embedding = (
        validate_embedding(
            unpack_embedding(embedding_blob),
            expected_dimensions=expected_dimensions,
        )
        if include_embedding and embedding_blob is not None
        else None
    )
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
