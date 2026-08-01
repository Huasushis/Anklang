"""Anklang 相似度标定命令行入口。

示例（真实文件必须位于被 Git 忽略且权限为 0700 的工作目录）：

    python3 -m anklang.calibrate \
      --dataset problems-data/calibration/dataset.json \
      --corpus-manifest problems-data/calibration/corpus.json \
      --label public-baseline-20260801

命令只调用所选检索后端，不调用 ``anklang.review``，因此不会启用 LLM 复核或改变线上
阈值。环境变量仍按 README 的方式由进程管理器传入，不读取或 source 任何 .env 文件。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

from .backends.local_engine import LocalEngineBackend
from .calibration import (
    DEFAULT_WORKSPACE,
    CalibrationError,
    CalibrationSettings,
    CorpusArtifactEvidence,
    run_calibration,
)
from .config import AppConfig, ConfigError, load_config
from .embedding import EmbeddingClient
from .server import build_backend
from .store import (
    INDEX_BUILD_REVISION,
    NO_EMBEDDING_MODEL,
    EmbeddingIndexSpec,
    IndexMetadata,
    IndexMetadataError,
    SearchSnapshot,
    StoredProblem,
    calculate_corpus_revision,
    parse_index_metadata,
)
from .vectormath import unpack_embedding, validate_embedding


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行只产出安全聚合证据的 Anklang 相似度标定。"
    )
    parser.add_argument("--dataset", required=True, help="私有人工标注数据集 JSON")
    parser.add_argument(
        "--corpus-manifest", required=True, help="私有语料来源与版本清单 JSON"
    )
    parser.add_argument("--label", required=True, help="唯一实验标签")
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_WORKSPACE),
        help="私有标定工作目录（默认 problems-data/calibration）",
    )
    parser.add_argument(
        "--resume", action="store_true", help="恢复同标签、同五类绑定的未完成检查点"
    )
    parser.add_argument(
        "--allow-external-statements",
        action="store_true",
        help="本次明确允许把数据集题面发送给当前配置的外部检索或向量服务",
    )
    parser.add_argument(
        "--top-k",
        action="append",
        type=int,
        dest="top_ks",
        help="登记一个 Recall@K；可重复填写，默认 1、5、8",
    )
    parser.add_argument(
        "--display-threshold",
        type=float,
        default=0.5,
        help="计算候选假阳性率时使用的显示下限",
    )
    parser.add_argument(
        "--candidate-threshold",
        action="append",
        type=float,
        dest="candidate_thresholds",
        help="预先登记一个待评估拦截阈值；可重复填写",
    )
    parser.add_argument(
        "--selection-k",
        type=int,
        default=8,
        help="选择阈值时最多查看的候选数量",
    )
    parser.add_argument(
        "--minimum-recall",
        type=float,
        default=0.95,
        help="校准集和留出集必须同时达到的最低召回率",
    )
    parser.add_argument(
        "--maximum-false-block-rate",
        type=float,
        default=0.05,
        help="校准集和留出集允许的最高假拦截率",
    )
    return parser


def _settings(args: argparse.Namespace) -> CalibrationSettings:
    defaults = CalibrationSettings()
    top_ks = tuple(args.top_ks) if args.top_ks else defaults.top_ks
    thresholds = (
        tuple(args.candidate_thresholds)
        if args.candidate_thresholds
        else defaults.candidate_thresholds
    )
    return CalibrationSettings(
        top_ks=top_ks,
        display_threshold=args.display_threshold,
        candidate_thresholds=thresholds,
        selection_k=args.selection_k,
        minimum_recall=args.minimum_recall,
        maximum_false_block_rate=args.maximum_false_block_rate,
    )


def _backend_descriptor(
    config: AppConfig, *, allow_external_statements: bool
) -> dict[str, Any]:
    """只登记影响检索的非秘密配置；地址仅登记摘要，绝不登记密钥。"""

    descriptor: dict[str, Any] = {
        "backend": config.backend,
        "searchK": config.search_k,
        "onlineMinimumSimilarity": config.minimum_similarity,
        "externalStatementTransferRequired": (
            _requires_external_statement_transfer(config)
        ),
        "externalStatementTransferExplicitlyAllowed": allow_external_statements,
    }
    if config.backend == "local_engine":
        descriptor.update(
            {
                "corpusBindingMode": "immutable-read-only-sqlite",
                "databasePathHash": _text_hash(config.local_db_path),
                "vectorTopK": config.local_vector_top_k,
                "keywordTopK": config.local_keyword_top_k,
                "embeddingConfigured": bool(
                    config.dashscope_base_url and config.dashscope_api_key
                ),
                "embeddingEndpointHash": (
                    _text_hash(config.dashscope_base_url)
                    if config.dashscope_base_url
                    else None
                ),
                "embeddingModel": config.dashscope_embedding_model,
                "embeddingDimensions": config.dashscope_embedding_dim,
            }
        )
    else:
        descriptor.update(
            {
                "corpusBindingMode": "unverifiable-remote-snapshot",
                "upstreamEndpointHash": _text_hash(config.yuantiji_base_url),
                "useRerank": config.use_rerank,
                "upstreamTimeoutSeconds": config.yuantiji_timeout_seconds,
                "upstreamMinimumIntervalSeconds": (
                    config.yuantiji_minimum_interval_seconds
                ),
                "upstreamMaximumRetries": config.yuantiji_max_retries,
                "upstreamRetryBaseDelaySeconds": (
                    config.yuantiji_retry_base_delay_seconds
                ),
                "upstreamCircuitFailureThreshold": (
                    config.yuantiji_circuit_failure_threshold
                ),
                "upstreamCircuitOpenSeconds": config.yuantiji_circuit_open_seconds,
            }
        )
    return descriptor


def _requires_external_statement_transfer(config: AppConfig) -> bool:
    return config.backend == "reverse_proxy" or bool(
        config.dashscope_base_url and config.dashscope_api_key
    )


def _open_pinned_readonly_sqlite(
    path: Path | str, expected_content_hash: str
) -> tuple[sqlite3.Connection, int]:
    """按摘要钉住数据库 inode，再让 SQLite 从该描述符只读打开。"""

    absolute = Path(os.path.abspath(path))
    try:
        before = absolute.lstat()
    except OSError as error:
        raise CalibrationError("语料快照数据库不存在或无法读取。") from error
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute, flags)
    except OSError as error:
        raise CalibrationError("无法安全打开语料快照数据库。") from error
    connection: sqlite3.Connection | None = None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
            or not stat.S_IMODE(opened.st_mode) & stat.S_IRUSR
            or stat.S_IMODE(opened.st_mode) & stat.S_IXUSR
        ):
            raise CalibrationError("语料快照数据库在打开前发生变化。")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (
            final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or digest.hexdigest() != expected_content_hash
        ):
            raise CalibrationError("语料快照数据库摘要已经变化。")
        os.lseek(descriptor, 0, os.SEEK_SET)
        uri = f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection, descriptor
    except BaseException:
        try:
            if connection is not None:
                connection.close()
        finally:
            os.close(descriptor)
        raise


class _ReadOnlyProblemStore:
    """标定专用 SQLite 读取器；不会建表、迁移、写 WAL 或更新题库。"""

    def __init__(self, path: Path | str, expected_content_hash: str) -> None:
        self._connection, self._descriptor = _open_pinned_readonly_sqlite(
            path, expected_content_hash
        )

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            os.close(self._descriptor)

    def count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM problems"
        ).fetchone()
        return int(row["count"])

    def iter_all(self) -> list[StoredProblem]:
        rows = self._connection.execute(
            "SELECT id, source, external_id, title, url, statement, embedding, "
            "content_hash, source_updated_at, created_at, updated_at FROM problems"
        ).fetchall()
        return [_readonly_problem(row, include_embedding=True) for row in rows]

    def search_snapshot(self, spec: EmbeddingIndexSpec | None) -> SearchSnapshot:
        rows = self._connection.execute(
            "SELECT id, source, external_id, title, url, statement, embedding, "
            "content_hash, source_updated_at, created_at, updated_at FROM problems"
        ).fetchall()
        keyword = tuple(
            _readonly_problem(row, include_embedding=False) for row in rows
        )
        try:
            metadata = _read_formal_index_metadata(self._connection)
            if metadata is None:
                status = (
                    "legacy_vectors"
                    if any(row["embedding"] is not None for row in rows)
                    else "uninitialized"
                )
                return SearchSnapshot(keyword, False, status)
            if spec is None:
                validate_vectors = metadata.embedding_model != NO_EMBEDDING_MODEL
                expected_dimensions = (
                    metadata.embedding_dimensions if validate_vectors else None
                )
            else:
                if metadata.embedding_model != spec.model:
                    raise IndexMetadataError("model_mismatch")
                if metadata.embedding_dimensions != spec.dimensions:
                    raise IndexMetadataError("dimension_mismatch")
                if metadata.index_build_revision != spec.build_revision:
                    raise IndexMetadataError("build_mismatch")
                validate_vectors = True
                expected_dimensions = spec.dimensions
            embedding_rows, corpus_revision = _readonly_actual_index_state(
                rows,
                expected_dimensions=expected_dimensions,
                validate_vectors=validate_vectors,
            )
            if (
                spec is None
                and metadata.index_build_revision != INDEX_BUILD_REVISION
            ):
                raise IndexMetadataError("build_mismatch")
            if (
                metadata.problem_count != len(rows)
                or metadata.embedding_rows != embedding_rows
                or metadata.corpus_revision != corpus_revision
            ):
                raise IndexMetadataError("metadata_stale")
            if spec is None and validate_vectors:
                try:
                    EmbeddingIndexSpec(
                        metadata.embedding_model,
                        metadata.embedding_dimensions,
                    )
                except ValueError as error:
                    raise IndexMetadataError("metadata_shape") from error
            status = _readonly_complete_status(metadata, spec)
            if status != "ready":
                return SearchSnapshot(keyword, False, status)
            assert spec is not None
            problems = tuple(
                _readonly_problem(
                    row,
                    include_embedding=True,
                    expected_dimensions=spec.dimensions,
                )
                for row in rows
            )
        except (IndexMetadataError, sqlite3.Error, ValueError) as error:
            status = (
                error.status
                if isinstance(error, IndexMetadataError)
                else "invalid_vectors"
            )
            return SearchSnapshot(keyword, False, status)
        return SearchSnapshot(problems, True, "ready")


def _read_formal_index_metadata(
    connection: sqlite3.Connection,
) -> IndexMetadata | None:
    if not _has_formal_index_metadata_schema(connection):
        raise IndexMetadataError("metadata_shape")
    metadata_rows = connection.execute(
        "SELECT key, value FROM index_metadata"
    ).fetchall()
    if not metadata_rows:
        return None
    metadata_values = {
        str(row["key"]): str(row["value"])
        for row in metadata_rows
    }
    if len(metadata_values) != len(metadata_rows):
        raise IndexMetadataError("metadata_shape")
    return parse_index_metadata(metadata_values)


def _readonly_actual_index_state(
    rows: list[sqlite3.Row],
    *,
    expected_dimensions: int | None,
    validate_vectors: bool,
) -> tuple[int, str]:
    embedding_rows = 0
    for row in rows:
        blob = row["embedding"]
        if blob is None:
            continue
        embedding_rows += 1
        if validate_vectors:
            validate_embedding(
                unpack_embedding(blob),
                expected_dimensions=expected_dimensions,
            )
    corpus_revision = calculate_corpus_revision(
        (
            str(row["source"]),
            str(row["external_id"]),
            str(row["content_hash"]),
        )
        for row in rows
    )
    return embedding_rows, corpus_revision


def _readonly_complete_status(
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


def _readonly_problem(
    row: sqlite3.Row,
    *,
    include_embedding: bool,
    expected_dimensions: int | None = None,
) -> StoredProblem:
    blob = row["embedding"]
    embedding = (
        validate_embedding(
            unpack_embedding(blob), expected_dimensions=expected_dimensions
        )
        if include_embedding and blob is not None
        else None
    )
    return StoredProblem(
        id=int(row["id"]),
        source=str(row["source"]),
        external_id=str(row["external_id"]),
        title=str(row["title"]),
        url=str(row["url"]) if row["url"] is not None else None,
        statement=str(row["statement"]),
        embedding=embedding,
        content_hash=str(row["content_hash"]),
        source_updated_at=(
            str(row["source_updated_at"])
            if row["source_updated_at"] is not None
            else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _corpus_verifier(
    config: AppConfig,
    verified_artifact: dict[str, str] | None = None,
) -> Any:
    if config.backend == "reverse_proxy":

        def verify_remote(evidence: CorpusArtifactEvidence) -> bool:
            if evidence.kind != "remote-snapshot":
                raise CalibrationError("反向代理标定必须登记远端语料快照。")
            # 当前接口无法证明远端检索期间实际使用了这份快照。
            return False

        return verify_remote

    def verify_local(evidence: CorpusArtifactEvidence) -> bool:
        if evidence.kind != "anklang-sqlite-v1":
            raise CalibrationError("本地检索标定必须使用 Anklang SQLite 快照。")
        configured_path = Path(os.path.abspath(config.local_db_path))
        if configured_path != evidence.path:
            raise CalibrationError("本地检索数据库必须就是清单绑定的语料快照。")
        try:
            connection, descriptor = _open_pinned_readonly_sqlite(
                configured_path, evidence.content_hash
            )
        except sqlite3.Error as error:
            raise CalibrationError("无法只读打开语料快照数据库。") from error
        try:
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(problems)")
            }
            required_columns = {
                "id",
                "source",
                "external_id",
                "title",
                "url",
                "statement",
                "embedding",
                "content_hash",
                "source_updated_at",
                "created_at",
                "updated_at",
            }
            if not required_columns.issubset(columns):
                raise CalibrationError("语料快照数据库结构不完整。")
            problem_count = int(
                connection.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
            )
            embedding_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM problems WHERE embedding IS NOT NULL"
                ).fetchone()[0]
            )
            if (
                problem_count != evidence.problem_count
                or embedding_rows != evidence.embedding_rows
            ):
                raise CalibrationError("语料快照数据库数量与清单不一致。")

            if verified_artifact is not None:
                verified_artifact["contentHash"] = evidence.content_hash
            try:
                metadata = _read_formal_index_metadata(connection)
            except (sqlite3.Error, IndexMetadataError):
                return False
            if metadata is None:
                return False

            corpus_revision = calculate_corpus_revision(
                (
                    str(row["source"]),
                    str(row["external_id"]),
                    str(row["content_hash"]),
                )
                for row in connection.execute(
                    "SELECT source, external_id, content_hash FROM problems"
                )
            )
            if (
                metadata.problem_count != problem_count
                or metadata.embedding_rows != embedding_rows
                or metadata.corpus_revision != corpus_revision
                or metadata.index_build_revision != INDEX_BUILD_REVISION
            ):
                return False

            observed_dimensions: set[int] = set()
            try:
                for row in connection.execute(
                    "SELECT embedding FROM problems WHERE embedding IS NOT NULL"
                ):
                    vector = validate_embedding(
                        unpack_embedding(row[0]),
                        expected_dimensions=metadata.embedding_dimensions,
                    )
                    observed_dimensions.add(len(vector))
            except ValueError as error:
                raise CalibrationError("语料快照包含无法核对的向量。") from error
            if embedding_rows == 0:
                if (
                    metadata.embedding_model != NO_EMBEDDING_MODEL
                    or metadata.embedding_dimensions != 0
                    or any(
                        value is not None
                        for value in (
                            evidence.embedding_model,
                            evidence.embedding_dimensions,
                            evidence.index_build_revision,
                        )
                    )
                ):
                    return False
            else:
                if (
                    embedding_rows != problem_count
                    or metadata.embedding_model == NO_EMBEDDING_MODEL
                    or evidence.embedding_model != metadata.embedding_model
                    or evidence.embedding_dimensions
                    != metadata.embedding_dimensions
                    or evidence.index_build_revision
                    != metadata.index_build_revision
                    or observed_dimensions != {metadata.embedding_dimensions}
                ):
                    return False
            if config.dashscope_base_url and config.dashscope_api_key:
                if (
                    metadata.embedding_model != config.dashscope_embedding_model
                    or metadata.embedding_dimensions != config.dashscope_embedding_dim
                ):
                    return False
            return True
        except CalibrationError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise CalibrationError("无法核对语料快照数据库。") from error
        finally:
            try:
                connection.close()
            finally:
                os.close(descriptor)

    return verify_local


def _has_formal_index_metadata_schema(connection: sqlite3.Connection) -> bool:
    try:
        columns = connection.execute("PRAGMA table_info(index_metadata)").fetchall()
    except sqlite3.Error:
        return False
    return [
        (
            str(row["name"]),
            str(row["type"]).upper(),
            int(row["notnull"]),
            int(row["pk"]),
        )
        for row in columns
    ] == [
        ("key", "TEXT", 0, 1),
        ("value", "TEXT", 1, 0),
    ]


def _build_calibration_backend(
    config: AppConfig, *, expected_artifact_hash: str | None = None
) -> Any:
    if config.backend != "local_engine":
        return build_backend(config)
    if expected_artifact_hash is None:
        raise CalibrationError("本地语料快照尚未完成绑定核对。")
    store = _ReadOnlyProblemStore(config.local_db_path, expected_artifact_hash)
    embedder: EmbeddingClient | None = None
    if config.dashscope_api_key and config.dashscope_base_url:
        embedder = EmbeddingClient(
            base_url=config.dashscope_base_url,
            api_key=config.dashscope_api_key,
            model=config.dashscope_embedding_model,
            dimensions=config.dashscope_embedding_dim,
        )
    return LocalEngineBackend(
        store=store,  # type: ignore[arg-type]
        embedder=embedder,
        vector_top_k=config.local_vector_top_k,
        keyword_top_k=config.local_keyword_top_k,
    )


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    backend: Any | None = None
    try:
        config = load_config()
        chosen_settings = _settings(args)
        # 在创建后端前先验证指标设置，避免无效实验意外打开数据库或发起请求。
        chosen_settings.descriptor()
        if max(chosen_settings.top_ks) > config.search_k:
            raise CalibrationError(
                "Recall@K 不能超过线上配置的 ANKLANG_SEARCH_K。"
            )
        external_transfer_required = _requires_external_statement_transfer(config)
        if external_transfer_required and not args.allow_external_statements:
            raise CalibrationError(
                "当前后端会把题面发送到外部服务；本次必须显式添加 "
                "--allow-external-statements。"
            )
        if chosen_settings.selection_k != config.search_k:
            raise CalibrationError(
                "阈值选择使用的 K 必须等于线上配置的 ANKLANG_SEARCH_K。"
            )
        if chosen_settings.display_threshold != config.minimum_similarity:
            raise CalibrationError(
                "候选显示下限必须等于线上配置的 ANKLANG_MINIMUM_SIMILARITY。"
            )
        verified_artifact: dict[str, str] = {}
        corpus_verifier = _corpus_verifier(config, verified_artifact)

        def search(statement: str, _maximum: int) -> list[dict[str, Any]]:
            nonlocal backend
            if backend is None:
                # 核心层已经验证私有输入、许可证清单并写好初始检查点后，才打开
                # 数据库或构造外部后端。无效输入不会产生数据库迁移或网络调用。
                backend = _build_calibration_backend(
                    config,
                    expected_artifact_hash=verified_artifact.get("contentHash"),
                )
            # 使用线上配置的候选数量运行，再由证据层裁剪到登记的最大 K；某些上游
            # 会根据 k 改变检索过程，不能用另一个 k 冒充线上行为。
            result = backend.search(statement, config.search_k)
            if result.status != "complete":
                # 部分检索失败不能作为完整标定样本；固定错误由核心层转换成 error。
                raise RuntimeError("标定检索结果不完整。")
            return result.candidates

        report = run_calibration(
            workspace=Path(args.workspace),
            dataset_path=Path(args.dataset),
            corpus_manifest_path=Path(args.corpus_manifest),
            label=args.label,
            backend_descriptor=_backend_descriptor(
                config,
                allow_external_statements=args.allow_external_statements,
            ),
            search=search,
            settings=chosen_settings,
            corpus_verifier=corpus_verifier,
            resume=args.resume,
        )
    except KeyboardInterrupt:
        sys.stderr.write(
            "标定中断；检查点已保留。恢复时不会重发状态不明的当前样本。\n"
        )
        return 130
    except (CalibrationError, ConfigError) as error:
        # 这些错误信息全部由本项目固定生成，不包含输入正文或外部原始响应。
        sys.stderr.write(f"标定失败：{error}\n")
        return 2
    except Exception:
        # 未知异常可能携带题面、路径、响应或密钥，绝不回显异常对象。
        sys.stderr.write("标定失败：发生未预期的本地错误。\n")
        return 2
    finally:
        store = getattr(backend, "store", None)
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    statuses = report["counts"]["statuses"]
    sys.stderr.write(
        "标定结束："
        f"complete={'true' if report['complete'] else 'false'}，"
        f"总样本 {report['counts']['total']}，成功 {statuses['success']}，"
        f"失败或缺失 {report['counts']['total'] - statuses['success']}。\n"
    )
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
