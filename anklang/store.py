"""SQLite rows and source cursors for the preserved upstream search flow.

The query entrypoint rebuilds its in-memory vector view from these current rows on every
request, so committed source updates are immediately visible.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .metadata import MetadataContractError, parse_canonical_metadata, to_canonical_json
from .vectormath import pack_embedding, unpack_embedding, validate_embedding


class IndexMetadataError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class EmbeddingIndexSpec:
    model: str
    dimensions: int

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("向量模型名不能为空。")
        if isinstance(self.dimensions, bool) or not isinstance(self.dimensions, int) or self.dimensions < 1:
            raise ValueError("向量维度必须是正整数。")


@dataclass(frozen=True)
class IndexMetadata:
    embedding_model: str
    embedding_dimensions: int
    embedding_base_url: str | None = None


@dataclass(frozen=True)
class StoredProblem:
    source: str
    external_id: str
    title: str
    url: str | None
    statement: str
    embedding: list[float] | None
    content_hash: str
    source_updated_at: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class SearchSnapshot:
    problems: tuple[StoredProblem, ...]
    vector_ready: bool
    vector_status: str


@dataclass(frozen=True)
class IndexInspection:
    problem_count: int
    vector_ready: bool
    status: str
    metadata: IndexMetadata | None


AddProblemOutcome = Literal["inserted", "updated", "unchanged", "stale"]


def compare_updated_at(first: str | None, second: str | None) -> int:
    """按 UTC 时刻比较版本时间，兼容不同合法小数精度的表示。"""

    if first == second:
        return 0
    if first is None:
        return -1
    if second is None:
        return 1
    try:
        first_time = datetime.fromisoformat(first[:-1] + "+00:00")
        second_time = datetime.fromisoformat(second[:-1] + "+00:00")
    except (TypeError, ValueError):
        # 旧数据库中的非规范值只能使用原有字典序，不能让比较本身泄漏异常。
        return -1 if first < second else 1
    if first_time < second_time:
        return -1
    return 1


_SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    statement TEXT NOT NULL,
    embedding BLOB,
    content_hash TEXT NOT NULL DEFAULT '',
    source_updated_at TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(source, external_id)
);
CREATE TABLE IF NOT EXISTS source_cursors (
    source TEXT PRIMARY KEY,
    cursor TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS index_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_rebuild_staging (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY(source, external_id)
);
"""


class ProblemStore:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 30000")
            self._conn.executescript(_SCHEMA)
            self._migrate_legacy_columns()
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate_legacy_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(problems)").fetchall()
        }
        additions = {
            "content_hash": "TEXT NOT NULL DEFAULT ''",
            "source_updated_at": "TEXT",
            "metadata": "TEXT",
            "updated_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE problems ADD COLUMN {name} {declaration}")
        self._conn.execute(
            "UPDATE problems SET updated_at = COALESCE(updated_at, created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        )
        self._conn.execute(
            "DELETE FROM index_metadata WHERE key NOT IN "
            "('embedding_model', 'embedding_dimensions', 'embedding_base_url')"
        )

    def _metadata_locked(self) -> IndexMetadata | None:
        rows = {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM index_metadata")
        }
        model = rows.get("embedding_model")
        dimensions = rows.get("embedding_dimensions")
        if model is None and dimensions is None:
            return None
        if not model or dimensions is None:
            raise IndexMetadataError("invalid_metadata", "向量索引元数据不完整。")
        try:
            parsed_dimensions = int(dimensions)
        except (TypeError, ValueError) as error:
            raise IndexMetadataError("invalid_metadata", "向量索引维度不合法。") from error
        if parsed_dimensions < 1:
            raise IndexMetadataError("invalid_metadata", "向量索引维度不合法。")
        base_url = rows.get("embedding_base_url")
        if base_url is not None and not base_url:
            raise IndexMetadataError("invalid_metadata", "向量索引提供方地址不合法。")
        return IndexMetadata(model, parsed_dimensions, base_url)

    @staticmethod
    def _require_matching_metadata(metadata: IndexMetadata, spec: EmbeddingIndexSpec) -> None:
        if metadata.embedding_model != spec.model:
            raise IndexMetadataError("model_mismatch", "向量模型与索引不一致。")
        if metadata.embedding_dimensions != spec.dimensions:
            raise IndexMetadataError("dimension_mismatch", "向量维度与索引不一致。")

    def prepare_embedding_writes(
        self,
        spec: EmbeddingIndexSpec,
        *,
        base_url: str | None = None,
    ) -> IndexMetadata:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                metadata = self._metadata_locked()
                if metadata is None:
                    embedded = int(
                        self._conn.execute(
                            "SELECT COUNT(*) FROM problems WHERE embedding IS NOT NULL"
                        ).fetchone()[0]
                    )
                    if embedded:
                        raise IndexMetadataError(
                            "unknown_vectors",
                            "已有向量缺少模型身份，不能安全追加。",
                        )
                    metadata = IndexMetadata(spec.model, spec.dimensions, base_url)
                else:
                    self._require_matching_metadata(metadata, spec)
                # Clean cutover from the superseded metadata state machine: persist only the
                # provider identity needed to prevent incompatible vector mixing.
                self._conn.execute("DELETE FROM index_metadata")
                self._conn.executemany(
                    "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
                    (
                        ("embedding_model", metadata.embedding_model),
                        ("embedding_dimensions", str(metadata.embedding_dimensions)),
                        *(
                            (("embedding_base_url", metadata.embedding_base_url),)
                            if metadata.embedding_base_url is not None
                            else ()
                        ),
                    ),
                )
                self._conn.commit()
                return metadata
            except Exception:
                self._conn.rollback()
                raise

    def _row_to_problem(
        self,
        row: sqlite3.Row,
        *,
        read_embedding: bool = True,
        read_metadata: bool = True,
    ) -> StoredProblem:
        blob = row["embedding"]
        metadata = (
            parse_canonical_metadata(row["metadata"])
            if read_metadata
            else None
        )
        return StoredProblem(
            source=row["source"],
            external_id=row["external_id"],
            title=row["title"],
            url=row["url"],
            statement=row["statement"],
            embedding=unpack_embedding(blob) if read_embedding and blob is not None else None,
            content_hash=row["content_hash"],
            source_updated_at=row["source_updated_at"],
            metadata=metadata,
        )

    def search_snapshot(self, spec: EmbeddingIndexSpec | None) -> SearchSnapshot:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, external_id, title, url, statement, embedding, content_hash, source_updated_at, metadata "
                "FROM problems ORDER BY id"
            ).fetchall()
            if not rows:
                return SearchSnapshot((), False, "empty")
            if spec is None:
                problems = tuple(
                    self._row_to_problem(row, read_embedding=False, read_metadata=False)
                    for row in rows
                )
                return SearchSnapshot(problems, False, "embedding_disabled")
            metadata = self._metadata_locked()
            if metadata is None:
                problems = tuple(
                    self._row_to_problem(row, read_embedding=False, read_metadata=False)
                    for row in rows
                )
                return SearchSnapshot(problems, False, "missing_metadata")
            try:
                self._require_matching_metadata(metadata, spec)
                problems = tuple(self._row_to_problem(row) for row in rows)
                for problem in problems:
                    if problem.embedding is None:
                        raise IndexMetadataError("incomplete_vectors", "题库存在缺失向量。")
                    validate_embedding(problem.embedding, expected_dimensions=spec.dimensions)
            except MetadataContractError:
                safe = tuple(
                    self._row_to_problem(row, read_embedding=False, read_metadata=False)
                    for row in rows
                )
                return SearchSnapshot(safe, False, "invalid_metadata")
            except IndexMetadataError as error:
                safe = tuple(
                    self._row_to_problem(row, read_embedding=False, read_metadata=False)
                    for row in rows
                )
                return SearchSnapshot(safe, False, error.status)
            except ValueError:
                safe = tuple(
                    self._row_to_problem(row, read_embedding=False, read_metadata=False)
                    for row in rows
                )
                return SearchSnapshot(safe, False, "invalid_vectors")
            return SearchSnapshot(problems, True, "ready")

    def inspect_index(self, spec: EmbeddingIndexSpec | None) -> IndexInspection:
        snapshot = self.search_snapshot(spec)
        with self._lock:
            metadata = self._metadata_locked()
        return IndexInspection(len(snapshot.problems), snapshot.vector_ready, snapshot.vector_status, metadata)

    def add_problem(
        self,
        problem: StoredProblem,
        *,
        index_spec: EmbeddingIndexSpec | None = None,
    ) -> AddProblemOutcome:
        vector = None
        if problem.embedding is not None:
            if index_spec is None:
                raise IndexMetadataError("missing_spec", "写入向量必须声明模型身份。")
            vector = validate_embedding(problem.embedding, expected_dimensions=index_spec.dimensions)
        blob = pack_embedding(vector) if vector is not None else None

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if vector is not None:
                    metadata = self._metadata_locked()
                    if metadata is None:
                        raise IndexMetadataError("missing_metadata", "向量索引尚未登记模型身份。")
                    self._require_matching_metadata(metadata, index_spec)
                existing = self._conn.execute(
                    "SELECT title, url, statement, embedding, content_hash, source_updated_at, metadata "
                    "FROM problems WHERE source = ? AND external_id = ?",
                    (problem.source, problem.external_id),
                ).fetchone()
                if existing is None:
                    self._conn.execute(
                        "INSERT INTO problems(source, external_id, title, url, statement, embedding, content_hash, source_updated_at, metadata) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            problem.source,
                            problem.external_id,
                            problem.title,
                            problem.url,
                            problem.statement,
                            blob,
                            problem.content_hash,
                            problem.source_updated_at,
                            to_canonical_json(problem.metadata) if problem.metadata else None,
                        ),
                    )
                    self._conn.commit()
                    return "inserted"

                old_timestamp = existing["source_updated_at"]
                new_timestamp = problem.source_updated_at
                existing_metadata = parse_canonical_metadata(existing["metadata"])
                same_payload = (
                    existing["title"] == problem.title
                    and existing["url"] == problem.url
                    and existing["statement"] == problem.statement
                    and existing["content_hash"] == problem.content_hash
                    and existing_metadata == problem.metadata
                )
                timestamp_comparison = compare_updated_at(
                    new_timestamp, old_timestamp
                )
                if old_timestamp is not None and (
                    new_timestamp is None
                    or timestamp_comparison < 0
                    or (timestamp_comparison == 0 and not same_payload)
                ):
                    self._conn.rollback()
                    return "stale"
                if old_timestamp is None and new_timestamp is None and not same_payload:
                    self._conn.rollback()
                    return "stale"
                if same_payload and timestamp_comparison == 0 and (
                    blob is None or existing["embedding"] == blob
                ):
                    self._conn.rollback()
                    return "unchanged"

                next_blob = blob
                if next_blob is None and existing["content_hash"] == problem.content_hash:
                    next_blob = existing["embedding"]
                self._conn.execute(
                    "UPDATE problems SET title = ?, url = ?, statement = ?, embedding = ?, content_hash = ?, "
                    "source_updated_at = ?, metadata = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE source = ? AND external_id = ?",
                    (
                        problem.title,
                        problem.url,
                        problem.statement,
                        next_blob,
                        problem.content_hash,
                        problem.source_updated_at,
                        to_canonical_json(problem.metadata) if problem.metadata else None,
                        problem.source,
                        problem.external_id,
                    ),
                )
                self._conn.commit()
                return "updated"
            except Exception:
                self._conn.rollback()
                raise

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0])

    def index_matches_provider(self, spec: EmbeddingIndexSpec, base_url: str) -> bool:
        """Return whether existing vectors are bound to this non-secret provider identity."""

        with self._lock:
            metadata = self._metadata_locked()
            if metadata is None:
                return False
            try:
                self._require_matching_metadata(metadata, spec)
            except IndexMetadataError:
                return False
            return metadata.embedding_base_url == base_url

    def begin_embedding_rebuild(self) -> int:
        """Clear an unused staging area and return the bounded rebuild snapshot size."""

        with self._lock:
            self._conn.execute("DELETE FROM embedding_rebuild_staging")
            total = int(self._conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0])
            self._conn.commit()
            return total

    def embedding_rebuild_batch(self, offset: int, limit: int) -> list[StoredProblem]:
        if offset < 0 or limit < 1 or limit > 100:
            raise ValueError("重建批次范围不合法。")
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, external_id, title, url, statement, NULL AS embedding, "
                "content_hash, source_updated_at, NULL AS metadata "
                "FROM problems ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [
            self._row_to_problem(row, read_embedding=False, read_metadata=False)
            for row in rows
        ]

    def stage_embedding_rebuild_batch(
        self,
        rows: list[tuple[str, str, str, list[float]]],
        spec: EmbeddingIndexSpec,
    ) -> None:
        packed = [
            (
                source,
                external_id,
                content_hash,
                pack_embedding(
                    validate_embedding(vector, expected_dimensions=spec.dimensions)
                ),
            )
            for source, external_id, content_hash, vector in rows
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO embedding_rebuild_staging"
                "(source, external_id, content_hash, embedding) VALUES (?, ?, ?, ?)",
                packed,
            )
            self._conn.commit()

    def finalize_embedding_rebuild(
        self,
        spec: EmbeddingIndexSpec,
        *,
        base_url: str,
    ) -> bool:
        """Atomically swap a complete, content-bound staged vector set into service."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                problem_count = int(
                    self._conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
                )
                staged_count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM embedding_rebuild_staging"
                    ).fetchone()[0]
                )
                mismatch = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM problems AS p "
                        "LEFT JOIN embedding_rebuild_staging AS s "
                        "ON s.source = p.source AND s.external_id = p.external_id "
                        "WHERE s.source IS NULL OR s.content_hash <> p.content_hash"
                    ).fetchone()[0]
                )
                if problem_count != staged_count or mismatch != 0:
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    "UPDATE problems SET embedding = ("
                    "SELECT s.embedding FROM embedding_rebuild_staging AS s "
                    "WHERE s.source = problems.source AND s.external_id = problems.external_id"
                    "), updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                )
                self._conn.execute("DELETE FROM index_metadata")
                self._conn.executemany(
                    "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
                    (
                        ("embedding_model", spec.model),
                        ("embedding_dimensions", str(spec.dimensions)),
                        ("embedding_base_url", base_url),
                    ),
                )
                self._conn.execute("DELETE FROM embedding_rebuild_staging")
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def abort_embedding_rebuild(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM embedding_rebuild_staging")
            self._conn.commit()

    def get_problem(self, source: str, external_id: str) -> StoredProblem | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT source, external_id, title, url, statement, embedding, content_hash, source_updated_at, metadata "
                "FROM problems WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
        return None if row is None else self._row_to_problem(row)

    def iter_all(self) -> list[StoredProblem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, external_id, title, url, statement, embedding, content_hash, source_updated_at, metadata "
                "FROM problems ORDER BY id"
            ).fetchall()
        return [self._row_to_problem(row) for row in rows]

    def get_cursor(self, source: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM source_cursors WHERE source = ?",
                (source,),
            ).fetchone()
        return None if row is None else row["cursor"]

    def compare_and_advance_cursor(
        self,
        source: str,
        expected_cursor: str | None,
        cursor: str,
    ) -> bool:
        if expected_cursor is not None and cursor <= expected_cursor:
            return False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT cursor FROM source_cursors WHERE source = ?",
                    (source,),
                ).fetchone()
                current = None if row is None else row["cursor"]
                if current != expected_cursor or (current is not None and cursor <= current):
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    "INSERT INTO source_cursors(source, cursor) VALUES (?, ?) "
                    "ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                    (source, cursor),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise
