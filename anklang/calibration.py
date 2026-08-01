"""Anklang 相似度标定的私有证据骨架。

本模块不改变线上检索、判定或拦截行为，只负责：

* 从 ``problems-data/calibration`` 读取权限受限的人工标注数据；
* 用调用方提供的检索函数逐条运行，并在每条结束后保存可恢复检查点；
* 把数据、设置、代码、后端和语料清单的摘要绑定到同一份报告；
* 只输出不含题面、标题、链接、候选明细或模型原话的聚合结果。

真实题面和逐条候选只允许出现在被 Git 忽略、权限为仅当前用户可读写的私有输入与
检查点中。即使检索函数抛出的异常包含这些内容，本模块也只记录固定状态，不保存异常
类型或文字。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "1"
DEFAULT_WORKSPACE = Path("problems-data/calibration")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_CASE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential|cookie|authorization)",
    re.IGNORECASE,
)
_DATASET_MAX_BYTES = 256 * 1024 * 1024
_CORPUS_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_CORPUS_ARTIFACT_MAX_BYTES = 64 * 1024 * 1024 * 1024
_CHECKPOINT_MAX_BYTES = 64 * 1024 * 1024
_MAX_STATEMENT_CHARACTERS = 500_000
_MAX_CANDIDATES_PER_SAMPLE = 200
_MAX_EVALUATION_K = 50
_MIN_CASES_PER_CLASS_PER_SPLIT = 100

SampleStatus = Literal["success", "error", "missing", "skipped", "cancelled"]
Split = Literal["calibration", "holdout"]


class CalibrationError(RuntimeError):
    """标定输入、私有文件或恢复证据不符合约定。"""


class SampleSkipped(RuntimeError):
    """检索调用方明确跳过当前样本；报告必须判为不完整。"""


class SampleCancelled(RuntimeError):
    """检索调用方明确取消当前样本；报告必须判为不完整。"""


@dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    split: Split
    statement: str = field(repr=False)
    expected_duplicate_candidates: tuple[tuple[str, str], ...]

    @property
    def is_positive(self) -> bool:
        return bool(self.expected_duplicate_candidates)


@dataclass(frozen=True)
class CalibrationDataset:
    cases: tuple[CalibrationCase, ...]


@dataclass(frozen=True)
class CorpusArtifactEvidence:
    kind: str
    path: Path = field(repr=False)
    content_hash: str
    problem_count: int
    embedding_rows: int
    embedding_model: str | None
    embedding_dimensions: int | None
    index_build_revision: str | None


@dataclass(frozen=True)
class CorpusManifest:
    problem_count: int
    artifact_file_name: str
    artifact_kind: str
    artifact_content_hash: str
    embedding_rows: int
    embedding_model: str | None
    embedding_dimensions: int | None
    index_build_revision: str | None


@dataclass(frozen=True)
class CalibrationSettings:
    """一次实验预先登记的指标与阈值选择规则。"""

    top_ks: tuple[int, ...] = (1, 5, 8)
    display_threshold: float = 0.5
    candidate_thresholds: tuple[float, ...] = (
        0.50,
        0.55,
        0.60,
        0.65,
        0.70,
        0.75,
        0.80,
        0.85,
        0.90,
        0.95,
    )
    selection_k: int = 8
    minimum_recall: float = 0.95
    maximum_false_block_rate: float = 0.05
    minimum_positive_cases_per_split: int = 100
    minimum_negative_cases_per_split: int = 100

    def descriptor(self) -> dict[str, Any]:
        _validate_settings(self)
        return {
            "topKs": list(self.top_ks),
            "displayThreshold": self.display_threshold,
            "candidateThresholds": list(self.candidate_thresholds),
            "selectionK": self.selection_k,
            "minimumRecall": self.minimum_recall,
            "maximumFalseBlockRate": self.maximum_false_block_rate,
            "minimumPositiveCasesPerSplit": self.minimum_positive_cases_per_split,
            "minimumNegativeCasesPerSplit": self.minimum_negative_cases_per_split,
        }


SearchFunction = Callable[[str, int], Sequence[Mapping[str, Any]] | None]
CorpusVerifier = Callable[[CorpusArtifactEvidence], bool]


def run_calibration(
    *,
    workspace: Path | str,
    dataset_path: Path | str,
    corpus_manifest_path: Path | str,
    label: str,
    backend_descriptor: Mapping[str, Any],
    search: SearchFunction,
    settings: CalibrationSettings | None = None,
    corpus_verifier: CorpusVerifier | None = None,
    resume: bool = False,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """运行或恢复一次标定，返回已经安全聚合的报告。

    ``search`` 只接收题面和最多候选数。它可以在 CLI 中包装真实后端，也可以在测试
    中使用完全离线的合成实现。返回值里的标题、链接、说明等字段一律丢弃，只保留
    ``source``、``externalId`` 和 ``similarity`` 到权限为 0600 的私有检查点。
    """

    chosen_settings = settings or CalibrationSettings()
    settings_descriptor = chosen_settings.descriptor()
    _validate_label(label)
    root = _require_private_directory(Path(workspace), "标定工作目录")
    dataset_file = _require_workspace_input(root, Path(dataset_path), "标定数据集")
    corpus_file = _require_workspace_input(
        root, Path(corpus_manifest_path), "语料清单"
    )

    dataset_bytes = _read_private_file(
        dataset_file, _DATASET_MAX_BYTES, "标定数据集"
    )
    corpus_bytes = _read_private_file(
        corpus_file, _CORPUS_MANIFEST_MAX_BYTES, "语料清单"
    )
    dataset = _parse_dataset(dataset_bytes)
    corpus_manifest = _validate_corpus_manifest(corpus_bytes)
    safe_backend_descriptor = _validate_backend_descriptor(backend_descriptor)
    artifact_file = _require_workspace_input(
        root,
        root / corpus_manifest.artifact_file_name,
        "语料快照",
    )
    artifact_hash, artifact_size = _hash_private_file(
        artifact_file, _CORPUS_ARTIFACT_MAX_BYTES, "语料快照"
    )
    if artifact_hash != corpus_manifest.artifact_content_hash:
        raise CalibrationError("语料快照摘要与语料清单不一致。")
    if artifact_size <= 0:
        raise CalibrationError("语料快照不能为空。")
    corpus_evidence = CorpusArtifactEvidence(
        kind=corpus_manifest.artifact_kind,
        path=artifact_file,
        content_hash=artifact_hash,
        problem_count=corpus_manifest.problem_count,
        embedding_rows=corpus_manifest.embedding_rows,
        embedding_model=corpus_manifest.embedding_model,
        embedding_dimensions=corpus_manifest.embedding_dimensions,
        index_build_revision=corpus_manifest.index_build_revision,
    )
    threshold_evidence_eligible = _verify_corpus_evidence(
        corpus_evidence,
        safe_backend_descriptor,
        corpus_verifier,
    )
    safe_backend_descriptor = dict(safe_backend_descriptor)
    safe_backend_descriptor["thresholdEvidenceEligible"] = (
        threshold_evidence_eligible
    )
    code_hash = compute_code_hash()
    bindings = {
        "datasetHash": _sha256(dataset_bytes),
        "configHash": _hash_json(settings_descriptor),
        "codeHash": code_hash,
        "backendHash": _hash_json(safe_backend_descriptor),
        "corpusHash": _hash_json(
            {
                "manifestHash": _sha256(corpus_bytes),
                "artifactHash": artifact_hash,
            }
        ),
    }

    run_directory, checkpoint_path, report_path = _prepare_run_directory(
        root, label, resume=resume
    )
    with _acquire_run_lock(run_directory) as run_lock:
        _ensure_name_absent(run_lock.directory_fd, report_path.name, "实验报告")
        if resume:
            checkpoint = _load_checkpoint(
                checkpoint_path,
                directory_fd=run_lock.directory_fd,
                label=label,
                bindings=bindings,
                dataset=dataset,
            )
        else:
            checkpoint = _new_checkpoint(label, bindings)
            _write_json_replacing(
                checkpoint_path,
                checkpoint,
                directory_fd=run_lock.directory_fd,
            )

        # 进程可能在付费请求已经到达服务端后、终态落盘前退出。恢复时绝不能自动
        # 重发这种状态不明的样本；把它固定记为 cancelled，报告自然会判为不完整。
        active_case_id = checkpoint["activeCase"]
        if active_case_id is not None:
            checkpoint["observations"].append(
                _terminal_observation(active_case_id, "cancelled")
            )
            checkpoint["activeCase"] = None
            _write_json_replacing(
                checkpoint_path,
                checkpoint,
                directory_fd=run_lock.directory_fd,
            )

        completed_case_ids = {
            observation["caseId"] for observation in checkpoint["observations"]
        }
        for case in dataset.cases:
            if case.case_id in completed_case_ids:
                continue
            checkpoint["activeCase"] = case.case_id
            _write_json_replacing(
                checkpoint_path,
                checkpoint,
                directory_fd=run_lock.directory_fd,
            )
            started = clock()
            try:
                raw_candidates = search(case.statement, max(chosen_settings.top_ks))
                finished = clock()
                if raw_candidates is None:
                    observation = _terminal_observation(case.case_id, "missing")
                else:
                    candidates = _normalize_candidates(
                        raw_candidates, maximum=max(chosen_settings.top_ks)
                    )
                    observation = {
                        "caseId": case.case_id,
                        "status": "success",
                        "latencyMs": _latency_ms(started, finished),
                        "candidates": candidates,
                    }
            except SampleSkipped:
                observation = _terminal_observation(case.case_id, "skipped")
            except SampleCancelled:
                observation = _terminal_observation(case.case_id, "cancelled")
            except KeyboardInterrupt:
                # activeCase 已经持久化。恢复时会把它判为 cancelled，而不是猜测
                # 服务端是否完成并再次发送题面。
                raise
            except Exception:
                # 异常可能包含题面、私有地址或上游原始响应，绝不能写进检查点或报告。
                observation = _terminal_observation(case.case_id, "error")

            checkpoint["observations"].append(observation)
            checkpoint["activeCase"] = None
            _write_json_replacing(
                checkpoint_path,
                checkpoint,
                directory_fd=run_lock.directory_fd,
            )

        # 运行期间若语料快照发生变化，这次候选已经不再绑定到同一份不可变语料。
        # 保留检查点但拒绝发布报告，调用方需换新标签重新实验。
        final_artifact_hash, final_artifact_size = _hash_private_file(
            artifact_file, _CORPUS_ARTIFACT_MAX_BYTES, "语料快照"
        )
        if (
            final_artifact_hash != artifact_hash
            or final_artifact_size != artifact_size
        ):
            _invalidate_checkpoint(
                checkpoint_path,
                checkpoint,
                directory_fd=run_lock.directory_fd,
            )
            raise CalibrationError("语料快照在实验期间发生变化。")
        if compute_code_hash() != code_hash:
            _invalidate_checkpoint(
                checkpoint_path,
                checkpoint,
                directory_fd=run_lock.directory_fd,
            )
            raise CalibrationError("Anklang 源码在实验期间发生变化。")

        try:
            run_lock.verify_path_identity()
        except CalibrationError:
            _invalidate_checkpoint(
                checkpoint_path,
                checkpoint,
                directory_fd=run_lock.directory_fd,
            )
            raise
        report = _build_report(
            label=label,
            bindings=bindings,
            dataset=dataset,
            observations=checkpoint["observations"],
            settings=chosen_settings,
            threshold_evidence_eligible=threshold_evidence_eligible,
        )
        _write_json_once(
            report_path,
            report,
            directory_fd=run_lock.directory_fd,
        )
        run_lock.verify_path_identity()
        os.fsync(run_lock.directory_fd)
        return report


def compute_code_hash() -> str:
    """计算当前 ``anklang`` Python 源码的稳定摘要，不依赖 Git 或工作树状态。"""

    package_root = Path(__file__).resolve().parent
    files = sorted(package_root.rglob("*.py"), key=lambda path: path.as_posix())
    if not files:
        raise CalibrationError("没有找到可绑定的 Anklang 源码。")
    digest = hashlib.sha256()
    for path in files:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise CalibrationError("Anklang 源码必须是普通文件。")
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _validate_settings(settings: CalibrationSettings) -> None:
    if not settings.top_ks or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in settings.top_ks
    ):
        raise CalibrationError("Recall@K 的 K 必须是正整数。")
    if tuple(sorted(set(settings.top_ks))) != settings.top_ks:
        raise CalibrationError("Recall@K 的 K 必须严格递增且不能重复。")
    if max(settings.top_ks) > _MAX_EVALUATION_K:
        raise CalibrationError("Recall@K 不能超过 50。")
    if settings.selection_k not in settings.top_ks:
        raise CalibrationError("阈值选择使用的 K 必须包含在 Recall@K 列表中。")
    _require_rate(settings.display_threshold, "候选显示下限")
    if not settings.candidate_thresholds:
        raise CalibrationError("至少需要登记一个候选拦截阈值。")
    for threshold in settings.candidate_thresholds:
        _require_rate(threshold, "候选拦截阈值")
        if threshold < settings.display_threshold:
            raise CalibrationError("候选拦截阈值不能低于候选显示下限。")
    if tuple(sorted(set(settings.candidate_thresholds))) != settings.candidate_thresholds:
        raise CalibrationError("候选拦截阈值必须严格递增且不能重复。")
    _require_rate(settings.minimum_recall, "最低召回率")
    _require_rate(settings.maximum_false_block_rate, "最高假拦截率")
    for count, description in (
        (settings.minimum_positive_cases_per_split, "每组最低正例数"),
        (settings.minimum_negative_cases_per_split, "每组最低反例数"),
    ):
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < _MIN_CASES_PER_CLASS_PER_SPLIT
        ):
            raise CalibrationError(f"{description}不能少于 100。")


def _require_rate(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationError(f"{name}必须是 0 到 1 之间的有限数字。")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise CalibrationError(f"{name}必须是 0 到 1 之间的有限数字。")
    return number


def _validate_label(label: object) -> None:
    if not isinstance(label, str) or _LABEL_RE.fullmatch(label) is None:
        raise CalibrationError("实验标签只能使用小写字母、数字、点、下划线和连字符。")


def _require_private_directory(path: Path, description: str) -> Path:
    absolute = Path(os.path.abspath(path))
    _reject_symlink_components(absolute, description)
    try:
        metadata = absolute.lstat()
    except OSError as error:
        raise CalibrationError(f"{description}不存在或无法读取。") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CalibrationError(f"{description}必须是非符号链接目录。")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CalibrationError(f"{description}必须仅允许当前用户访问。")
    return absolute


def _require_workspace_input(root: Path, path: Path, description: str) -> Path:
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(path))
    _reject_symlink_components(path.parent, f"{description}所在目录")
    parent = path.parent
    if parent != root:
        raise CalibrationError(f"{description}必须直接放在标定工作目录中。")
    # 不能用 resolve() 得到最终文件，否则会先跟随符号链接。
    return parent / path.name


def _reject_symlink_components(path: Path, description: str) -> None:
    """拒绝路径任一层经符号链接跳转，不能只检查最后一层文件。"""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise CalibrationError(f"{description}不存在或无法读取。") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise CalibrationError(f"{description}不能经过符号链接。")


def _read_private_file(path: Path, maximum_bytes: int, description: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise CalibrationError(f"{description}不存在或无法读取。") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CalibrationError(f"{description}必须是普通文件，不能是符号链接。")
    mode = stat.S_IMODE(before.st_mode)
    if (
        before.st_uid != os.geteuid()
        or mode & 0o077
        or not mode & stat.S_IRUSR
        or mode & stat.S_IXUSR
    ):
        raise CalibrationError(f"{description}必须仅允许当前用户读取或写入。")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CalibrationError(f"{description}无法安全打开。") from error
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
        ):
            raise CalibrationError(f"{description}在读取前发生变化。")
        if after.st_size <= 0 or after.st_size > maximum_bytes:
            raise CalibrationError(f"{description}大小不符合限制。")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum_bytes:
            raise CalibrationError(f"{description}大小不符合限制。")
        return content
    finally:
        os.close(descriptor)


def _read_private_file_at(
    directory_fd: int,
    name: str,
    maximum_bytes: int,
    description: str,
) -> bytes:
    """相对已打开目录读取私有文件，避免路径目录在检查后被替换。"""

    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise CalibrationError(f"{description}不存在或无法读取。") from error
    mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or mode & 0o077
        or not mode & stat.S_IRUSR
        or mode & stat.S_IXUSR
    ):
        raise CalibrationError(f"{description}必须是权限安全的普通文件。")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise CalibrationError(f"{description}无法安全打开。") from error
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size <= 0
            or after.st_size > maximum_bytes
        ):
            raise CalibrationError(f"{description}在读取前发生变化。")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            len(content) > maximum_bytes
            or final.st_size != len(content)
            or final.st_mtime_ns != after.st_mtime_ns
        ):
            raise CalibrationError(f"{description}在读取期间发生变化。")
        return content
    finally:
        os.close(descriptor)


def _hash_private_file(
    path: Path, maximum_bytes: int, description: str
) -> tuple[str, int]:
    """流式计算可能很大的语料快照摘要，不把题库整体读入内存。"""

    try:
        before = path.lstat()
    except OSError as error:
        raise CalibrationError(f"{description}不存在或无法读取。") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CalibrationError(f"{description}必须是普通文件，不能是符号链接。")
    mode = stat.S_IMODE(before.st_mode)
    if (
        before.st_uid != os.geteuid()
        or mode & 0o077
        or not mode & stat.S_IRUSR
        or mode & stat.S_IXUSR
    ):
        raise CalibrationError(f"{description}必须仅允许当前用户读取或写入。")
    if before.st_size <= 0 or before.st_size > maximum_bytes:
        raise CalibrationError(f"{description}大小不符合限制。")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CalibrationError(f"{description}无法安全打开。") from error
    digest = hashlib.sha256()
    total = 0
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
        ):
            raise CalibrationError(f"{description}在读取前发生变化。")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise CalibrationError(f"{description}大小不符合限制。")
            digest.update(chunk)
        final = os.fstat(descriptor)
        if final.st_size != total or final.st_mtime_ns != after.st_mtime_ns:
            raise CalibrationError(f"{description}在读取期间发生变化。")
        return digest.hexdigest(), total
    finally:
        os.close(descriptor)


def _parse_json_object(raw: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise CalibrationError(f"{description}不是有效的 JSON 对象。") from error
    if not isinstance(value, dict):
        raise CalibrationError(f"{description}必须是 JSON 对象。")
    return value


def _parse_dataset(raw: bytes) -> CalibrationDataset:
    value = _parse_json_object(raw, "标定数据集")
    if set(value) != {"schemaVersion", "datasetId", "cases"}:
        raise CalibrationError("标定数据集字段不完整或包含额外字段。")
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise CalibrationError("标定数据集版本不受支持。")
    _require_short_text(value.get("datasetId"), 200, "数据集编号")
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CalibrationError("标定数据集必须包含样本。")

    cases: list[CalibrationCase] = []
    seen_case_ids: set[str] = set()
    statement_splits: dict[str, Split] = {}
    expected_ids_by_split: dict[Split, set[str]] = {
        "calibration": set(),
        "holdout": set(),
    }
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict) or set(raw_case) != {
            "caseId",
            "split",
            "statement",
            "expectedDuplicateCandidates",
        }:
            raise CalibrationError("标定样本字段不完整或包含额外字段。")
        case_id = raw_case.get("caseId")
        if not isinstance(case_id, str) or _CASE_ID_RE.fullmatch(case_id) is None:
            raise CalibrationError("标定样本编号不合法。")
        if case_id in seen_case_ids:
            raise CalibrationError("标定数据集包含重复样本编号。")
        seen_case_ids.add(case_id)
        split = raw_case.get("split")
        if split not in {"calibration", "holdout"}:
            raise CalibrationError("标定样本必须属于 calibration 或 holdout。")
        statement = raw_case.get("statement")
        if (
            not isinstance(statement, str)
            or not statement.strip()
            or len(statement) > _MAX_STATEMENT_CHARACTERS
        ):
            raise CalibrationError("标定样本题面不合法。")
        raw_expected = raw_case.get("expectedDuplicateCandidates")
        if not isinstance(raw_expected, list):
            raise CalibrationError("人工确认的重复题身份必须是列表。")
        expected: list[tuple[str, str]] = []
        for raw_identity in raw_expected:
            if not isinstance(raw_identity, dict) or set(raw_identity) != {
                "source",
                "externalId",
            }:
                raise CalibrationError("人工确认的重复题身份不合法。")
            expected.append(
                (
                    _require_short_text(
                        raw_identity.get("source"), 80, "人工确认的重复题来源"
                    ),
                    _require_short_text(
                        raw_identity.get("externalId"),
                        200,
                        "人工确认的重复题编号",
                    ),
                )
            )
        if len(set(expected)) != len(expected):
            raise CalibrationError("同一样本不能重复登记候选题编号。")

        statement_hash = _sha256(statement.encode("utf-8"))
        previous_split = statement_splits.get(statement_hash)
        if previous_split is not None:
            if previous_split != split:
                raise CalibrationError("校准集和留出集不能包含相同题面。")
            raise CalibrationError("标定数据集不能重复使用相同题面。")
        statement_splits[statement_hash] = split
        other_split: Split = "holdout" if split == "calibration" else "calibration"
        expected_keys = {
            f"{source}\0{external_id}" for source, external_id in expected
        }
        if expected_ids_by_split[other_split].intersection(expected_keys):
            raise CalibrationError("校准集和留出集不能共享人工确认的重复题编号。")
        expected_ids_by_split[split].update(expected_keys)
        cases.append(
            CalibrationCase(
                case_id=case_id,
                split=split,
                statement=statement,
                expected_duplicate_candidates=tuple(expected),
            )
        )

    for split in ("calibration", "holdout"):
        split_cases = [case for case in cases if case.split == split]
        if not any(case.is_positive for case in split_cases) or not any(
            not case.is_positive for case in split_cases
        ):
            raise CalibrationError("校准集和留出集都必须同时包含正例与反例。")
    return CalibrationDataset(cases=tuple(cases))


def _validate_corpus_manifest(raw: bytes) -> CorpusManifest:
    value = _parse_json_object(raw, "语料清单")
    if set(value) != {
        "schemaVersion",
        "corpusId",
        "problemCount",
        "sources",
        "artifact",
    }:
        raise CalibrationError("语料清单字段不完整或包含额外字段。")
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise CalibrationError("语料清单版本不受支持。")
    _require_short_text(value.get("corpusId"), 200, "语料编号")
    problem_count = value.get("problemCount")
    if (
        isinstance(problem_count, bool)
        or not isinstance(problem_count, int)
        or problem_count <= 0
    ):
        raise CalibrationError("语料清单必须登记正数题目数量。")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CalibrationError("语料清单必须登记至少一个来源。")
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "sourceId",
            "revision",
            "license",
            "licenseReviewed",
            "provenance",
            "contentSha256",
        }:
            raise CalibrationError("语料来源字段不完整或包含额外字段。")
        source_id = _require_short_text(source.get("sourceId"), 200, "语料来源编号")
        if source_id in seen:
            raise CalibrationError("语料清单包含重复来源。")
        seen.add(source_id)
        _require_short_text(source.get("revision"), 500, "语料来源版本")
        _require_short_text(source.get("license"), 500, "语料许可证说明")
        _require_short_text(source.get("provenance"), 2_000, "语料来源记录")
        if source.get("licenseReviewed") is not True:
            raise CalibrationError("语料来源必须完成人工许可证复核。")
        content_hash = source.get("contentSha256")
        if not isinstance(content_hash, str) or _SHA256_RE.fullmatch(content_hash) is None:
            raise CalibrationError("语料来源内容摘要不合法。")

    artifact = value.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "kind",
        "fileName",
        "contentSha256",
        "problemCount",
        "embeddingRows",
        "embeddingModel",
        "embeddingDimensions",
        "indexBuildRevision",
    }:
        raise CalibrationError("语料快照字段不完整或包含额外字段。")
    kind = artifact.get("kind")
    if kind not in {
        "synthetic-fixture",
        "anklang-sqlite-v1",
        "remote-snapshot",
    }:
        raise CalibrationError("语料快照类型不受支持。")
    file_name = artifact.get("fileName")
    if (
        not isinstance(file_name, str)
        or _CASE_ID_RE.fullmatch(file_name) is None
        or file_name in {"dataset.json", "corpus.json"}
    ):
        raise CalibrationError("语料快照文件名不合法。")
    artifact_hash = artifact.get("contentSha256")
    if not isinstance(artifact_hash, str) or _SHA256_RE.fullmatch(artifact_hash) is None:
        raise CalibrationError("语料快照内容摘要不合法。")
    artifact_problem_count = artifact.get("problemCount")
    if artifact_problem_count != problem_count:
        raise CalibrationError("语料快照题目数量与语料清单不一致。")
    embedding_rows = artifact.get("embeddingRows")
    if (
        isinstance(embedding_rows, bool)
        or not isinstance(embedding_rows, int)
        or not 0 <= embedding_rows <= problem_count
    ):
        raise CalibrationError("语料快照向量数量不合法。")
    embedding_model = artifact.get("embeddingModel")
    embedding_dimensions = artifact.get("embeddingDimensions")
    index_build_revision = artifact.get("indexBuildRevision")
    if embedding_rows == 0:
        if any(
            item is not None
            for item in (
                embedding_model,
                embedding_dimensions,
                index_build_revision,
            )
        ):
            raise CalibrationError("没有向量的语料快照不能登记向量构建信息。")
    else:
        _require_short_text(embedding_model, 200, "向量模型")
        _require_short_text(index_build_revision, 200, "索引构建版本")
        if (
            isinstance(embedding_dimensions, bool)
            or not isinstance(embedding_dimensions, int)
            or not 1 <= embedding_dimensions <= 4096
        ):
            raise CalibrationError("语料快照向量维度不合法。")
    return CorpusManifest(
        problem_count=problem_count,
        artifact_file_name=file_name,
        artifact_kind=kind,
        artifact_content_hash=artifact_hash,
        embedding_rows=embedding_rows,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        index_build_revision=index_build_revision,
    )


def _verify_corpus_evidence(
    evidence: CorpusArtifactEvidence,
    backend_descriptor: dict[str, Any],
    verifier: CorpusVerifier | None,
) -> bool:
    if verifier is not None:
        try:
            return verifier(evidence) is True
        except CalibrationError:
            raise
        except Exception as error:
            raise CalibrationError("无法核对实际语料快照。") from error
    if evidence.kind == "synthetic-fixture" and backend_descriptor.get("backend") == "synthetic":
        raw = _read_private_file(
            evidence.path, _CORPUS_MANIFEST_MAX_BYTES, "合成语料快照"
        )
        value = _parse_json_object(raw, "合成语料快照")
        if set(value) != {"schemaVersion", "problemCount"}:
            raise CalibrationError("合成语料快照字段不合法。")
        if value.get("schemaVersion") != SCHEMA_VERSION or value.get(
            "problemCount"
        ) != evidence.problem_count:
            raise CalibrationError("合成语料快照数量与清单不一致。")
        if evidence.embedding_rows != 0:
            raise CalibrationError("合成语料快照不能冒充已构建向量索引。")
        return True
    # 远端题库没有可验证的不可变快照，或 SQLite 尚未由 CLI 做只读结构核对时，
    # 可以保留完整运行证据，但绝不能据此推荐线上拦截阈值。
    return False


def _require_short_text(value: object, maximum: int, description: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CalibrationError(f"{description}不合法。")
    return value


def _validate_backend_descriptor(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise CalibrationError("后端描述必须是非空对象。")

    def normalize(candidate: Any, path: tuple[str, ...]) -> Any:
        if isinstance(candidate, Mapping):
            normalized: dict[str, Any] = {}
            for key in sorted(candidate):
                if not isinstance(key, str) or not key or len(key) > 120:
                    raise CalibrationError("后端描述的字段名不合法。")
                if _SENSITIVE_KEY_RE.search(key):
                    raise CalibrationError("后端描述不能包含密钥或凭据字段。")
                normalized[key] = normalize(candidate[key], (*path, key))
            return normalized
        if isinstance(candidate, (list, tuple)):
            return [normalize(item, path) for item in candidate]
        if candidate is None or isinstance(candidate, (str, bool, int)):
            if isinstance(candidate, str) and len(candidate) > 2_000:
                raise CalibrationError("后端描述文本过长。")
            return candidate
        if isinstance(candidate, float) and math.isfinite(candidate):
            return candidate
        raise CalibrationError("后端描述只能包含普通 JSON 值。")

    normalized = normalize(value, ())
    encoded = _canonical_json(normalized)
    if len(encoded) > 64 * 1024:
        raise CalibrationError("后端描述过大。")
    return normalized


def _prepare_run_directory(
    root: Path, label: str, *, resume: bool
) -> tuple[Path, Path, Path]:
    runs = root / "runs"
    _create_or_validate_private_directory(runs, "标定运行目录")
    run_directory = runs / label
    if resume:
        try:
            run_directory.lstat()
        except OSError as error:
            raise CalibrationError("找不到可以恢复的标定检查点。") from error
        _require_private_directory(run_directory, "标定实验目录")
    else:
        try:
            run_directory.mkdir(mode=0o700)
        except FileExistsError as error:
            raise CalibrationError("实验标签已经存在，不能覆盖旧实验。") from error
        except OSError as error:
            raise CalibrationError("无法创建标定实验目录。") from error
        _require_private_directory(run_directory, "标定实验目录")

    checkpoint_path = run_directory / "checkpoint.json"
    report_path = run_directory / "report.json"
    if report_path.exists() or report_path.is_symlink():
        raise CalibrationError("实验报告已经存在，不能覆盖或恢复。")
    if resume and not checkpoint_path.exists():
        raise CalibrationError("找不到可以恢复的标定检查点。")
    return run_directory, checkpoint_path, report_path


def _create_or_validate_private_directory(path: Path, description: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _require_private_directory(path, description)
    except OSError as error:
        raise CalibrationError(f"{description}无法创建。") from error
    else:
        _require_private_directory(path, description)


@dataclass
class _RunDirectoryLock:
    path: Path
    directory_fd: int
    lock_fd: int
    device: int
    inode: int

    def __enter__(self) -> _RunDirectoryLock:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self.lock_fd)
            os.close(self.directory_fd)

    def verify_path_identity(self) -> None:
        try:
            current = self.path.lstat()
        except OSError as error:
            raise CalibrationError("标定实验目录在运行期间发生变化。") from error
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or current.st_dev != self.device
            or current.st_ino != self.inode
        ):
            raise CalibrationError("标定实验目录在运行期间发生变化。")


def _acquire_run_lock(path: Path) -> _RunDirectoryLock:
    """锁住一次实验，并持有目录描述符供后续 openat/renameat 风格操作。"""

    try:
        before = path.lstat()
    except OSError as error:
        raise CalibrationError("标定实验目录不存在或无法读取。") from error
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(path, flags)
    except OSError as error:
        raise CalibrationError("无法安全打开标定实验目录。") from error
    lock_fd: int | None = None
    try:
        opened = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise CalibrationError("标定实验目录在打开前发生变化。")
        lock_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        try:
            lock_fd = os.open("run.lock", lock_flags, 0o600, dir_fd=directory_fd)
        except OSError as error:
            raise CalibrationError("无法创建标定实验锁。") from error
        lock_metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise CalibrationError("标定实验锁必须是权限为 0600 的普通文件。")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            raise CalibrationError("同一实验正在由另一个进程运行。") from error
        return _RunDirectoryLock(
            path=path,
            directory_fd=directory_fd,
            lock_fd=lock_fd,
            device=opened.st_dev,
            inode=opened.st_ino,
        )
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)
        raise


def _ensure_name_absent(directory_fd: int, name: str, description: str) -> None:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise CalibrationError(f"无法检查{description}。") from error
    raise CalibrationError(f"{description}已经存在，不能覆盖或恢复。")


def _new_checkpoint(label: str, bindings: dict[str, str]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "label": label,
        "bindings": dict(bindings),
        "activeCase": None,
        "invalidated": False,
        "observations": [],
    }


def _load_checkpoint(
    path: Path,
    *,
    directory_fd: int,
    label: str,
    bindings: dict[str, str],
    dataset: CalibrationDataset,
) -> dict[str, Any]:
    raw = _read_private_file_at(
        directory_fd,
        path.name,
        _CHECKPOINT_MAX_BYTES,
        "标定检查点",
    )
    value = _parse_json_object(raw, "标定检查点")
    if set(value) != {
        "schemaVersion",
        "label",
        "bindings",
        "activeCase",
        "invalidated",
        "observations",
    }:
        raise CalibrationError("标定检查点字段不完整或包含额外字段。")
    if value.get("schemaVersion") != SCHEMA_VERSION or value.get("label") != label:
        raise CalibrationError("标定检查点身份不一致。")
    if value.get("bindings") != bindings:
        raise CalibrationError("标定检查点绑定已经变化，不能继续旧实验。")
    if not isinstance(value.get("invalidated"), bool):
        raise CalibrationError("标定检查点失效状态不合法。")
    if value["invalidated"]:
        raise CalibrationError("标定检查点已经失效，必须使用新实验标签。")
    observations = value.get("observations")
    if not isinstance(observations, list):
        raise CalibrationError("标定检查点的样本状态不合法。")
    if len(observations) > len(dataset.cases):
        raise CalibrationError("标定检查点包含多余样本。")
    normalized: list[dict[str, Any]] = []
    for index, raw_observation in enumerate(observations):
        expected_case_id = dataset.cases[index].case_id
        observation = _validate_observation(raw_observation)
        if observation["caseId"] != expected_case_id:
            raise CalibrationError("标定检查点的样本顺序不一致。")
        normalized.append(observation)
    active_case = value.get("activeCase")
    if active_case is not None:
        if (
            not isinstance(active_case, str)
            or len(normalized) >= len(dataset.cases)
            or active_case != dataset.cases[len(normalized)].case_id
        ):
            raise CalibrationError("标定检查点的进行中样本不合法。")
    value["observations"] = normalized
    return value


def _invalidate_checkpoint(
    path: Path,
    checkpoint: dict[str, Any],
    *,
    directory_fd: int,
) -> None:
    """永久封存运行期绑定变化的标签，防止恢复后误发完整报告。"""

    checkpoint["activeCase"] = None
    checkpoint["invalidated"] = True
    _write_json_replacing(path, checkpoint, directory_fd=directory_fd)


def _validate_observation(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CalibrationError("标定检查点包含无效样本状态。")
    status_value = value.get("status")
    if status_value == "success":
        if set(value) != {"caseId", "status", "latencyMs", "candidates"}:
            raise CalibrationError("成功样本的检查点字段不合法。")
        case_id = value.get("caseId")
        if not isinstance(case_id, str) or _CASE_ID_RE.fullmatch(case_id) is None:
            raise CalibrationError("检查点样本编号不合法。")
        latency = value.get("latencyMs")
        if (
            isinstance(latency, bool)
            or not isinstance(latency, (int, float))
            or not math.isfinite(float(latency))
            or float(latency) < 0
        ):
            raise CalibrationError("检查点耗时不合法。")
        candidates = _normalize_candidates(
            value.get("candidates"), maximum=_MAX_CANDIDATES_PER_SAMPLE
        )
        return {
            "caseId": case_id,
            "status": "success",
            "latencyMs": float(latency),
            "candidates": candidates,
        }
    if status_value not in {"error", "missing", "skipped", "cancelled"}:
        raise CalibrationError("检查点样本状态不受支持。")
    if set(value) != {"caseId", "status"}:
        raise CalibrationError("未完成样本的检查点字段不合法。")
    case_id = value.get("caseId")
    if not isinstance(case_id, str) or _CASE_ID_RE.fullmatch(case_id) is None:
        raise CalibrationError("检查点样本编号不合法。")
    return {"caseId": case_id, "status": status_value}


def _normalize_candidates(value: object, *, maximum: int) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CalibrationError("检索结果必须是候选列表。")
    if len(value) > _MAX_CANDIDATES_PER_SAMPLE:
        raise CalibrationError("检索结果候选数量超过标定限制。")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw_candidate in value:
        if not isinstance(raw_candidate, Mapping):
            raise CalibrationError("检索候选必须是对象。")
        source = raw_candidate.get("source")
        if not isinstance(source, str) or not source.strip() or len(source) > 80:
            raise CalibrationError("检索候选来源不合法。")
        external_id = raw_candidate.get("externalId")
        if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 200:
            raise CalibrationError("检索候选编号不合法。")
        identity = (source, external_id)
        if identity in seen:
            raise CalibrationError("检索结果包含重复候选身份。")
        seen.add(identity)
        similarity = _require_rate(raw_candidate.get("similarity"), "候选相似度")
        normalized.append(
            {"source": source, "externalId": external_id, "similarity": similarity}
        )
    # 线上 review 同样按相似度稳定排序；相同分数时必须保留后端原顺序，不能用候选
    # 身份另造一个只存在于标定程序里的 tie-break（同分排序规则）。
    normalized.sort(key=lambda item: -item["similarity"])
    return normalized[:maximum]


def _terminal_observation(case_id: str, status_value: SampleStatus) -> dict[str, str]:
    return {"caseId": case_id, "status": status_value}


def _latency_ms(started: float, finished: float) -> float:
    try:
        elapsed = (float(finished) - float(started)) * 1000.0
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(elapsed) or elapsed < 0:
        return 0.0
    return round(elapsed, 6)


def _build_report(
    *,
    label: str,
    bindings: dict[str, str],
    dataset: CalibrationDataset,
    observations: list[dict[str, Any]],
    settings: CalibrationSettings,
    threshold_evidence_eligible: bool,
) -> dict[str, Any]:
    status_counts = {
        "success": 0,
        "error": 0,
        "missing": 0,
        "skipped": 0,
        "cancelled": 0,
    }
    for observation in observations:
        status_value = observation["status"]
        status_counts[status_value] += 1
    unobserved = len(dataset.cases) - len(observations)
    if unobserved > 0:
        status_counts["missing"] += unobserved
    complete = (
        len(observations) == len(dataset.cases)
        and status_counts["success"] == len(dataset.cases)
    )
    successful = [
        observation for observation in observations if observation["status"] == "success"
    ]
    latency = _latency_summary(
        [float(observation["latencyMs"]) for observation in successful]
    )
    counts = {
        "total": len(dataset.cases),
        "calibration": sum(case.split == "calibration" for case in dataset.cases),
        "holdout": sum(case.split == "holdout" for case in dataset.cases),
        "statuses": status_counts,
    }

    metrics: dict[str, Any] | None = None
    recommendation: dict[str, Any] | None = None
    if complete:
        observation_by_case = {
            observation["caseId"]: observation for observation in observations
        }
        metrics = {
            split: _split_metrics(
                split=split,
                dataset=dataset,
                observation_by_case=observation_by_case,
                settings=settings,
            )
            for split in ("calibration", "holdout")
        }
        if threshold_evidence_eligible:
            recommendation = _recommend_threshold(metrics, settings)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "label": label,
        "complete": complete,
        "thresholdEvidenceEligible": threshold_evidence_eligible,
        "bindings": dict(bindings),
        "settings": settings.descriptor(),
        "counts": counts,
        "latencyMs": latency,
        "metrics": metrics,
        "thresholdRecommendation": recommendation,
    }


def _split_metrics(
    *,
    split: Split,
    dataset: CalibrationDataset,
    observation_by_case: dict[str, dict[str, Any]],
    settings: CalibrationSettings,
) -> dict[str, Any]:
    cases = [case for case in dataset.cases if case.split == split]
    positives = [case for case in cases if case.is_positive]
    negatives = [case for case in cases if not case.is_positive]

    recall_at_k: dict[str, float] = {}
    for k in settings.top_ks:
        hits = sum(
            _case_has_expected_candidate(
                case, observation_by_case[case.case_id]["candidates"], k=k
            )
            for case in positives
        )
        recall_at_k[str(k)] = hits / len(positives)

    recall_by_threshold: dict[str, float] = {}
    recall_hits_by_threshold: dict[str, int] = {}
    false_block_by_threshold: dict[str, float] = {}
    false_blocks_by_threshold: dict[str, int] = {}
    for threshold in settings.candidate_thresholds:
        key = _rate_key(threshold)
        positive_hits = sum(
            _case_has_expected_candidate(
                case,
                observation_by_case[case.case_id]["candidates"],
                k=settings.selection_k,
                threshold=threshold,
            )
            for case in positives
        )
        false_blocks = sum(
            _has_candidate_at_threshold(
                observation_by_case[case.case_id]["candidates"],
                k=settings.selection_k,
                threshold=threshold,
            )
            for case in negatives
        )
        recall_by_threshold[key] = positive_hits / len(positives)
        recall_hits_by_threshold[key] = positive_hits
        false_block_by_threshold[key] = false_blocks / len(negatives)
        false_blocks_by_threshold[key] = false_blocks

    false_positives = sum(
        _has_candidate_at_threshold(
            observation_by_case[case.case_id]["candidates"],
            k=settings.selection_k,
            threshold=settings.display_threshold,
        )
        for case in negatives
    )
    latencies = [
        float(observation_by_case[case.case_id]["latencyMs"]) for case in cases
    ]
    return {
        "positiveCases": len(positives),
        "negativeCases": len(negatives),
        "recallAtK": recall_at_k,
        "falsePositiveRateAtDisplayThreshold": false_positives / len(negatives),
        "recallAtSelectionKByThreshold": recall_by_threshold,
        "recallHitsAtSelectionKByThreshold": recall_hits_by_threshold,
        "falseBlockRateByThreshold": false_block_by_threshold,
        "falseBlockCasesByThreshold": false_blocks_by_threshold,
        "latencyMs": _latency_summary(latencies),
    }


def _case_has_expected_candidate(
    case: CalibrationCase,
    candidates: list[dict[str, Any]],
    *,
    k: int,
    threshold: float | None = None,
) -> bool:
    expected = set(case.expected_duplicate_candidates)
    for candidate in candidates[:k]:
        if threshold is not None and candidate["similarity"] < threshold:
            continue
        if (candidate["source"], candidate["externalId"]) in expected:
            return True
    return False


def _has_candidate_at_threshold(
    candidates: list[dict[str, Any]], *, k: int, threshold: float
) -> bool:
    return any(candidate["similarity"] >= threshold for candidate in candidates[:k])


def _recommend_threshold(
    metrics: dict[str, Any], settings: CalibrationSettings
) -> dict[str, Any] | None:
    calibration = metrics["calibration"]
    holdout = metrics["holdout"]
    for split_metrics in (calibration, holdout):
        if (
            split_metrics["positiveCases"]
            < settings.minimum_positive_cases_per_split
            or split_metrics["negativeCases"]
            < settings.minimum_negative_cases_per_split
        ):
            return None
    for threshold in settings.candidate_thresholds:
        key = _rate_key(threshold)
        calibration_recall = calibration["recallAtSelectionKByThreshold"][key]
        calibration_false_block = calibration["falseBlockRateByThreshold"][key]
        calibration_recall_lower = _wilson_lower_bound(
            calibration["recallHitsAtSelectionKByThreshold"][key],
            calibration["positiveCases"],
        )
        calibration_false_block_upper = _wilson_upper_bound(
            calibration["falseBlockCasesByThreshold"][key],
            calibration["negativeCases"],
        )
        if (
            calibration_recall_lower < settings.minimum_recall
            or calibration_false_block_upper
            > settings.maximum_false_block_rate
        ):
            continue
        holdout_recall = holdout["recallAtSelectionKByThreshold"][key]
        holdout_false_block = holdout["falseBlockRateByThreshold"][key]
        holdout_recall_lower = _wilson_lower_bound(
            holdout["recallHitsAtSelectionKByThreshold"][key],
            holdout["positiveCases"],
        )
        holdout_false_block_upper = _wilson_upper_bound(
            holdout["falseBlockCasesByThreshold"][key],
            holdout["negativeCases"],
        )
        if (
            holdout_recall_lower < settings.minimum_recall
            or holdout_false_block_upper > settings.maximum_false_block_rate
        ):
            return None
        return {
            "threshold": threshold,
            "selectionK": settings.selection_k,
            "selectedUsing": "calibration",
            "validatedUsing": "holdout",
            "calibrationRecall": calibration_recall,
            "calibrationRecallLower95": calibration_recall_lower,
            "calibrationFalseBlockRate": calibration_false_block,
            "calibrationFalseBlockUpper95": calibration_false_block_upper,
            "holdoutRecall": holdout_recall,
            "holdoutRecallLower95": holdout_recall_lower,
            "holdoutFalseBlockRate": holdout_false_block,
            "holdoutFalseBlockUpper95": holdout_false_block_upper,
        }
    return None


def _latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "samples": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "maximum": None,
        }
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "maximum": ordered[-1],
    }


def _percentile(ordered: list[float], proportion: float) -> float:
    index = max(0, math.ceil(len(ordered) * proportion) - 1)
    return ordered[index]


def _wilson_lower_bound(successes: int, total: int) -> float:
    center, margin, denominator = _wilson_components(successes, total)
    return max(0.0, (center - margin) / denominator)


def _wilson_upper_bound(successes: int, total: int) -> float:
    center, margin, denominator = _wilson_components(successes, total)
    return min(1.0, (center + margin) / denominator)


def _wilson_components(successes: int, total: int) -> tuple[float, float, float]:
    if total <= 0 or successes < 0 or successes > total:
        raise CalibrationError("置信区间样本计数不合法。")
    z = 1.959963984540054
    proportion = successes / total
    z_squared = z * z
    center = proportion + z_squared / (2 * total)
    margin = z * math.sqrt(
        (proportion * (1 - proportion) + z_squared / (4 * total)) / total
    )
    denominator = 1 + z_squared / total
    return center, margin, denominator


def _rate_key(value: float) -> str:
    return format(value, ".12g")


def _write_json_replacing(
    path: Path,
    value: dict[str, Any],
    *,
    directory_fd: int,
) -> None:
    try:
        metadata = os.stat(
            path.name, dir_fd=directory_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise CalibrationError("无法检查旧检查点。") from error
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise CalibrationError("标定检查点必须是权限为 0600 的普通文件。")
    temporary = _write_temporary_json(
        directory_fd, path.name, value
    )
    try:
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except OSError as error:
        raise CalibrationError("无法原子更新标定检查点。") from error
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _write_json_once(
    path: Path,
    value: dict[str, Any],
    *,
    directory_fd: int,
) -> None:
    _ensure_name_absent(directory_fd, path.name, "实验报告")
    temporary = _write_temporary_json(directory_fd, path.name, value)
    try:
        os.link(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileExistsError as error:
        raise CalibrationError("实验报告已经存在，不能覆盖。") from error
    except OSError as error:
        raise CalibrationError("无法原子发布标定报告。") from error
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _write_temporary_json(
    directory_fd: int, target_name: str, value: dict[str, Any]
) -> str:
    temporary = f".{target_name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    except OSError as error:
        raise CalibrationError("无法创建标定临时文件。") from error
    try:
        payload = _canonical_json(value) + b"\n"
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    return temporary


def _hash_json(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
