"""为 Fermata 审题流程采集可追溯的 Anklang v2 原始响应。

本模块只接受 Git 已忽略、权限受限的私有 manifest 和 request 文件。每个样本最多
发送一次；请求前先持久化 active 状态，进程若在结果落盘前退出，恢复时会把该样本
固定记为 cancelled，绝不猜测服务端状态并自动重发。

题面只会存在于私有输入、运行目录中的原始 request 副本和发往固定 Anklang 地址的
HTTP 请求中。终端输出、错误文本、attestation 都不包含题面、响应正文、令牌或地址。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .contracts import ContractError, parse_request, validate_v2_result


SCHEMA_VERSION = 2
MAX_CASE_COUNT = 2_000
COMPLETION_MARKER_NAME = "REVIEW_FLOW_ANKLANG_CAPTURE_COMPLETE"
ATTESTATION_NAME = "attestation.json"
CHECKPOINT_NAME = "checkpoint.json"
HEALTH_BEFORE_NAME = "health-before.response.json"
HEALTH_AFTER_NAME = "health-after.response.json"
FIXED_DEPENDENCY_PATHS = (
    "scripts/capture-review-flow-calibration.py",
    "anklang/__init__.py",
    "anklang/review_flow_capture.py",
    "anklang/contracts.py",
)
HISTORICAL_BACKENDS = frozenset({"reverse_proxy"})
RESULT_CONTRACT_KEYS = (
    "apiVersion",
    "contentHash",
    "checkedAt",
    "completion",
    "candidates",
)
_SERVICE_ALLOWED_CHANGE_PATHS = frozenset(
    {*FIXED_DEPENDENCY_PATHS, "tests/test_review_flow_capture.py"}
)

_CAPTURE_ID_RE = re.compile(r"^capture-[0-9a-f]{16}$")
_CASE_ID_RE = re.compile(r"^case-[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_FILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$")
_SAFE_TEMP_NAME_RE = re.compile(r"^\.[a-zA-Z0-9][a-zA-Z0-9._-]{0,239}$")
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_REQUEST_BYTES = 4_000_000
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
_MAX_ATTESTATION_BYTES = 8 * 1024 * 1024
_MAX_HEALTH_BYTES = 1 * 1024 * 1024

_OBSERVATION_STATUSES = {
    "complete",
    "http_error",
    "response_invalid",
    "response_incomplete",
    "transport_error",
    "cancelled",
    "missing",
}


class CaptureError(RuntimeError):
    """只携带固定错误码，避免异常链把私有内容带到终端。"""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", code):
            code = "CAPTURE_INTERNAL_ERROR"
        self.code = code
        super().__init__(code)


class CaptureCancelled(RuntimeError):
    """显式取消当前样本；该样本终态为 cancelled，且不会重发。"""


@dataclass(frozen=True)
class GeneratorIdentity:
    git_head: str
    runner_sha256: str
    dependency_code_sha256: str

    def descriptor(self) -> dict[str, Any]:
        return {
            "repository": "Anklang",
            "codeVersion": self.git_head,
            "runnerPath": FIXED_DEPENDENCY_PATHS[0],
            "runnerSha256": self.runner_sha256,
            "dependencyCodeSha256": self.dependency_code_sha256,
            "dependencyFileCount": len(FIXED_DEPENDENCY_PATHS),
        }


@dataclass(frozen=True)
class RequestBinding:
    case_id: str
    file_name: str
    expected_sha256: str
    request_id: str
    content_hash: str


@dataclass(frozen=True)
class CaptureManifest:
    capture_id: str
    service_code_version: str
    expected_case_count: int
    case_selection: dict[str, Any]
    endpoint: str
    base_url_sha256: str
    timeout_ms: int
    result_contract: dict[str, Any]
    corpus_identity: dict[str, Any]
    embedding_identity: dict[str, Any]
    index_identity: dict[str, Any]
    runtime_declaration: dict[str, Any]
    runtime_declaration_sha256: str
    request_identity_set_sha256: str
    cases: tuple[RequestBinding, ...]

@dataclass(frozen=True)
class HttpCaptureResponse:
    status: int
    content_type: str | None
    cache_control: str | None
    body: bytes | None


Transport = Callable[[str, str, bytes, float], HttpCaptureResponse]
HealthTransport = Callable[[str, str, float], HttpCaptureResponse]
Now = Callable[[], datetime]


@dataclass(frozen=True)
class HealthEvidence:
    sha256: str
    canonical_sha256: str
    metadata_sha256: str
    backend: str
    status: str


def run_capture(
    *,
    workspace: Path | str,
    manifest_path: Path | str,
    service_token: str,
    resume: bool = False,
    transport: Transport | None = None,
    health_transport: HealthTransport | None = None,
    now: Now | None = None,
    before_completion_marker: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """运行或恢复一个采集批次，返回不含题面和响应正文的安全摘要。"""

    token = _validate_service_token(service_token)
    generator = _load_generator_identity()
    repository_root = _repository_root()
    root = _require_private_directory(Path(workspace), "CAPTURE_WORKSPACE_INVALID")
    manifest_file = _require_workspace_input(
        root, Path(manifest_path), "CAPTURE_MANIFEST_LOCATION_INVALID"
    )
    _require_project_ignored(repository_root, manifest_file)
    manifest_bytes = _read_private_file(
        manifest_file, _MAX_MANIFEST_BYTES, "CAPTURE_MANIFEST_INVALID"
    )
    manifest_sha256 = _sha256(manifest_bytes)
    manifest = _parse_manifest(manifest_bytes, root, repository_root)
    _require_service_identity(
        repository_root,
        service_code_version=manifest.service_code_version,
        generator=generator,
    )
    _require_project_ignored(
        repository_root,
        root / "runs" / manifest.capture_id / "output-probe.private",
    )

    bindings = {
        "manifestSha256": manifest_sha256,
        "baseUrlSha256": manifest.base_url_sha256,
        "runtimeDeclarationSha256": manifest.runtime_declaration_sha256,
        "requestIdentitySetSha256": manifest.request_identity_set_sha256,
        "serviceCodeVersion": manifest.service_code_version,
        "generator": generator.descriptor(),
    }
    run_directory = _prepare_run_directory(root, manifest.capture_id, resume=resume)
    clock = now or (lambda: datetime.now(timezone.utc))

    with _acquire_run_lock(run_directory) as run_lock:
        health_call = health_transport or _get_health_once
        attestation_exists = _name_exists(run_lock.directory_fd, ATTESTATION_NAME)
        marker_exists = _name_exists(
            run_lock.directory_fd, COMPLETION_MARKER_NAME
        )
        if marker_exists:
            raise CaptureError("CAPTURE_ALREADY_COMPLETE")
        if attestation_exists:
            if not resume:
                raise CaptureError("CAPTURE_ATTESTATION_EXISTS")
            if not _name_exists(run_lock.directory_fd, CHECKPOINT_NAME):
                raise CaptureError("CAPTURE_CHECKPOINT_MISSING")
            checkpoint = _load_checkpoint(
                run_lock.directory_fd,
                manifest=manifest,
                bindings=bindings,
            )
            attestation_bytes = _read_private_file_at(
                run_lock.directory_fd,
                ATTESTATION_NAME,
                _MAX_ATTESTATION_BYTES,
                "CAPTURE_ATTESTATION_INVALID",
            )
            health_before = _read_health_snapshot(
                run_lock.directory_fd,
                HEALTH_BEFORE_NAME,
                expected_backend=manifest.runtime_declaration["backend"],
            )
            health_after = _read_health_snapshot(
                run_lock.directory_fd,
                HEALTH_AFTER_NAME,
                expected_backend=manifest.runtime_declaration["backend"],
            )
            _require_health_equal(health_before, health_after)
            attestation = _validate_existing_attestation(
                attestation_bytes,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                bindings=bindings,
                observations=checkpoint["observations"],
                run_directory_fd=run_lock.directory_fd,
                health_before=health_before,
                health_after=health_after,
            )
            final_generator = _load_generator_identity()
            if final_generator != generator:
                raise CaptureError("CAPTURE_CODE_CHANGED")
            if before_completion_marker is not None:
                before_completion_marker()
            _require_source_identity(
                manifest_file,
                manifest_sha256=manifest_sha256,
                generator=generator,
            )
            run_lock.verify_path_identity()
            _publish_completion_marker(
                run_lock.directory_fd,
                manifest=manifest,
                attestation_bytes=attestation_bytes,
            )
            return {
                "complete": True,
                "captureId": manifest.capture_id,
                "counts": {
                    "expected": manifest.expected_case_count,
                    "observed": manifest.expected_case_count,
                    "complete": manifest.expected_case_count,
                    "incomplete": 0,
                    "byStatus": {
                        status: (
                            manifest.expected_case_count
                            if status == "complete"
                            else 0
                        )
                        for status in sorted(_OBSERVATION_STATUSES)
                    },
                },
                "attestation": attestation,
            }

        checkpoint_exists = _name_exists(run_lock.directory_fd, CHECKPOINT_NAME)
        if resume:
            if not checkpoint_exists:
                raise CaptureError("CAPTURE_CHECKPOINT_MISSING")
            checkpoint = _load_checkpoint(
                run_lock.directory_fd,
                manifest=manifest,
                bindings=bindings,
            )
        else:
            if checkpoint_exists:
                raise CaptureError("CAPTURE_CHECKPOINT_EXISTS")
            checkpoint = _new_checkpoint(manifest.capture_id, bindings)
            _write_json_replacing(
                run_lock.directory_fd, CHECKPOINT_NAME, checkpoint
            )

        health_before = _read_or_capture_health_snapshot(
            run_lock.directory_fd,
            HEALTH_BEFORE_NAME,
            endpoint=manifest.endpoint,
            service_token=token,
            timeout_seconds=manifest.timeout_ms / 1000,
            expected_backend=manifest.runtime_declaration["backend"],
            allow_existing=resume,
            transport=health_call,
        )

        if checkpoint["active"] is not None:
            active = checkpoint["active"]
            binding = manifest.cases[active["caseIndex"]]
            checkpoint["observations"].append(
                _recover_cancelled_observation(
                    run_lock.directory_fd,
                    binding=binding,
                    case_index=active["caseIndex"],
                )
            )
            checkpoint["active"] = None
            _write_json_replacing(
                run_lock.directory_fd, CHECKPOINT_NAME, checkpoint
            )

        completed_case_ids = {
            observation["caseId"] for observation in checkpoint["observations"]
        }
        call = transport or _post_once
        for case_index, binding in enumerate(manifest.cases):
            if binding.case_id in completed_case_ids:
                continue
            checkpoint["active"] = {
                "caseId": binding.case_id,
                "caseIndex": case_index,
                "attempt": 1,
                "requestSha256": binding.expected_sha256,
            }
            _write_json_replacing(
                run_lock.directory_fd, CHECKPOINT_NAME, checkpoint
            )

            request_bytes = _read_bound_request(
                root, repository_root, binding
            )
            request_output = _request_output_name(case_index)
            response_output = _response_output_name(case_index)
            response_metadata_output = _response_metadata_output_name(case_index)
            _write_bytes_once(
                run_lock.directory_fd, request_output, request_bytes
            )

            try:
                response = call(
                    manifest.endpoint,
                    token,
                    request_bytes,
                    manifest.timeout_ms / 1000,
                )
            except CaptureCancelled:
                observation = _observation(
                    binding,
                    case_index,
                    status="cancelled",
                    http_status=None,
                    response_sha256=None,
                    checked_at=None,
                )
            except KeyboardInterrupt:
                # active 已经 fsync；恢复时固定取消，绝不自动重发。
                raise
            except Exception:
                observation = _observation(
                    binding,
                    case_index,
                    status="transport_error",
                    http_status=None,
                    response_sha256=None,
                    checked_at=None,
                )
            else:
                response_sha256: str | None = None
                if response.body is not None:
                    _write_bytes_once(
                        run_lock.directory_fd, response_output, response.body
                    )
                    response_sha256 = _sha256(response.body)
                response_metadata = _response_metadata(
                    binding,
                    case_index,
                    response,
                    response_sha256=response_sha256,
                )
                _write_bytes_once(
                    run_lock.directory_fd,
                    response_metadata_output,
                    _pretty_json(response_metadata),
                )
                observation = _evaluate_response(
                    binding,
                    case_index,
                    response,
                    response_sha256=response_sha256,
                )

            checkpoint["observations"].append(observation)
            checkpoint["active"] = None
            _write_json_replacing(
                run_lock.directory_fd, CHECKPOINT_NAME, checkpoint
            )

        if len(checkpoint["observations"]) != manifest.expected_case_count:
            raise CaptureError("CAPTURE_OBSERVATION_COUNT_INVALID")
        health_after = _read_or_capture_health_snapshot(
            run_lock.directory_fd,
            HEALTH_AFTER_NAME,
            endpoint=manifest.endpoint,
            service_token=token,
            timeout_seconds=manifest.timeout_ms / 1000,
            expected_backend=manifest.runtime_declaration["backend"],
            allow_existing=resume,
            transport=health_call,
        )
        _require_health_equal(health_before, health_after)
        if _sha256(
            _read_private_file(
                manifest_file, _MAX_MANIFEST_BYTES, "CAPTURE_MANIFEST_INVALID"
            )
        ) != manifest_sha256:
            checkpoint["invalidated"] = True
            _write_json_replacing(
                run_lock.directory_fd, CHECKPOINT_NAME, checkpoint
            )
            raise CaptureError("CAPTURE_MANIFEST_CHANGED")
        final_generator = _load_generator_identity()
        if final_generator != generator:
            checkpoint["invalidated"] = True
            _write_json_replacing(
                run_lock.directory_fd, CHECKPOINT_NAME, checkpoint
            )
            raise CaptureError("CAPTURE_CODE_CHANGED")
        run_lock.verify_path_identity()

        result = _build_capture_result(
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            bindings=bindings,
            observations=checkpoint["observations"],
            run_directory_fd=run_lock.directory_fd,
            created_at=_utc_z(clock()),
            health_before=health_before,
            health_after=health_after,
        )
        run_lock.verify_path_identity()
        if result["complete"]:
            attestation = result["attestation"]
            attestation_bytes = _pretty_json(attestation)
            _write_bytes_once(
                run_lock.directory_fd, ATTESTATION_NAME, attestation_bytes
            )
            if before_completion_marker is not None:
                before_completion_marker()
            _require_source_identity(
                manifest_file,
                manifest_sha256=manifest_sha256,
                generator=generator,
            )
            run_lock.verify_path_identity()
            _publish_completion_marker(
                run_lock.directory_fd,
                manifest=manifest,
                attestation_bytes=attestation_bytes,
            )
        return result


def verify_capture(
    *,
    workspace: Path | str,
    manifest_path: Path | str,
    expected_service_code_version: str,
    expected_code_version: str,
    expected_runner_sha256: str,
    expected_dependency_code_sha256: str,
) -> bytes:
    """只读重放一个已完成批次，成功时返回保存的 attestation 原始字节。"""

    generator = _load_generator_identity()
    _require_expected_generator_identity(
        generator,
        expected_code_version=expected_code_version,
        expected_runner_sha256=expected_runner_sha256,
        expected_dependency_code_sha256=expected_dependency_code_sha256,
    )
    repository_root = _repository_root()
    root = _require_private_directory(Path(workspace), "CAPTURE_WORKSPACE_INVALID")
    manifest_file = _require_workspace_input(
        root, Path(manifest_path), "CAPTURE_MANIFEST_LOCATION_INVALID"
    )
    _require_project_ignored(repository_root, manifest_file)
    manifest_bytes = _read_private_file(
        manifest_file, _MAX_MANIFEST_BYTES, "CAPTURE_MANIFEST_INVALID"
    )
    manifest_sha256 = _sha256(manifest_bytes)
    manifest = _parse_manifest(manifest_bytes, root, repository_root)
    _require_service_identity(
        repository_root,
        service_code_version=manifest.service_code_version,
        generator=generator,
    )
    if manifest.service_code_version != expected_service_code_version:
        raise CaptureError("CAPTURE_SERVICE_IDENTITY_MISMATCH")
    run_directory = _require_existing_run_directory(root, manifest.capture_id)
    _require_project_ignored(
        repository_root, run_directory / "output-probe.private"
    )
    bindings = {
        "manifestSha256": manifest_sha256,
        "baseUrlSha256": manifest.base_url_sha256,
        "runtimeDeclarationSha256": manifest.runtime_declaration_sha256,
        "requestIdentitySetSha256": manifest.request_identity_set_sha256,
        "serviceCodeVersion": manifest.service_code_version,
        "generator": generator.descriptor(),
    }

    with _acquire_existing_run_lock(run_directory) as run_lock:
        checkpoint = _load_checkpoint(
            run_lock.directory_fd,
            manifest=manifest,
            bindings=bindings,
        )
        if (
            checkpoint["active"] is not None
            or checkpoint["invalidated"] is not False
            or len(checkpoint["observations"])
            != manifest.expected_case_count
            or any(
                observation["status"] != "complete"
                for observation in checkpoint["observations"]
            )
        ):
            raise CaptureError("CAPTURE_CHECKPOINT_INCOMPLETE")

        health_before = _read_health_snapshot(
            run_lock.directory_fd,
            HEALTH_BEFORE_NAME,
            expected_backend=manifest.runtime_declaration["backend"],
        )
        health_after = _read_health_snapshot(
            run_lock.directory_fd,
            HEALTH_AFTER_NAME,
            expected_backend=manifest.runtime_declaration["backend"],
        )
        _require_health_equal(health_before, health_after)

        attestation_bytes = _read_private_file_at(
            run_lock.directory_fd,
            ATTESTATION_NAME,
            _MAX_ATTESTATION_BYTES,
            "CAPTURE_ATTESTATION_INVALID",
        )
        attestation = _validate_existing_attestation(
            attestation_bytes,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            bindings=bindings,
            observations=checkpoint["observations"],
            run_directory_fd=run_lock.directory_fd,
            health_before=health_before,
            health_after=health_after,
        )
        marker_bytes = _read_private_file_at(
            run_lock.directory_fd,
            COMPLETION_MARKER_NAME,
            1024 * 1024,
            "CAPTURE_COMPLETION_INVALID",
        )
        expected_marker = _completion_marker_value(
            manifest=manifest,
            attestation_bytes=attestation_bytes,
            attestation=attestation,
        )
        if marker_bytes != _pretty_json(expected_marker):
            raise CaptureError("CAPTURE_COMPLETION_INVALID")

        for binding in manifest.cases:
            _read_bound_request(root, repository_root, binding)
        _require_source_identity(
            manifest_file,
            manifest_sha256=manifest_sha256,
            generator=generator,
        )
        run_lock.verify_path_identity()
        final_generator = _load_generator_identity()
        if final_generator != generator:
            raise CaptureError("CAPTURE_CODE_CHANGED")
        _require_expected_generator_identity(
            final_generator,
            expected_code_version=expected_code_version,
            expected_runner_sha256=expected_runner_sha256,
            expected_dependency_code_sha256=expected_dependency_code_sha256,
        )
        return attestation_bytes


def verify_manifest(
    *,
    workspace: Path | str,
    manifest_path: Path | str,
    expected_service_code_version: str,
    expected_case_count: int = 32,
) -> bytes:
    """只读验证冻结 manifest 的服务、语料、索引和请求身份绑定。"""
    root = Path(workspace).resolve()
    repository_root = _repository_root()
    manifest_file = Path(manifest_path).resolve()
    _require_project_ignored(repository_root, manifest_file)
    raw = _read_private_file(
        manifest_file, _MAX_MANIFEST_BYTES, "CAPTURE_MANIFEST_INVALID"
    )
    manifest_sha256 = _sha256(raw)
    manifest = _parse_manifest(raw, root, repository_root)
    generator = _load_generator_identity()
    _require_service_identity(
        repository_root,
        service_code_version=manifest.service_code_version,
        generator=generator,
    )
    if (
        not isinstance(expected_service_code_version, str)
        or manifest.service_code_version != expected_service_code_version
    ):
        raise CaptureError("CAPTURE_SERVICE_IDENTITY_MISMATCH")
    if manifest.expected_case_count != expected_case_count:
        raise CaptureError("CAPTURE_CASE_COUNT_MISMATCH")
    return _pretty_json(
        {
            "complete": True,
            "captureId": manifest.capture_id,
            "manifestSha256": manifest_sha256,
            "serviceCodeVersion": manifest.service_code_version,
            "caseCount": manifest.expected_case_count,
            "caseSelection": manifest.case_selection,
            "corpus": {
                "sourceCount": manifest.corpus_identity["sourceCount"],
                "sourceSetSha256": manifest.corpus_identity["sourceSetSha256"],
                "requestSetSha256": manifest.corpus_identity["requestSetSha256"],
            },
            "embedding": {
                "provider": manifest.embedding_identity["provider"],
                "model": manifest.embedding_identity["model"],
                "dimensions": manifest.embedding_identity["dimensions"],
                "configured": manifest.embedding_identity["configured"],
                "configurationSha256": manifest.embedding_identity[
                    "configurationSha256"
                ],
            },
            "index": {
                "dbSha256": manifest.index_identity["dbSha256"],
                "problemCount": manifest.index_identity["problemCount"],
                "vectorIndexStatus": manifest.index_identity[
                    "vectorIndexStatus"
                ],
            },
            "resultContract": manifest.result_contract,
            "requestIdentitySetSha256": manifest.request_identity_set_sha256,
            "runtimeDeclaration": manifest.runtime_declaration,
        }
    )


def _require_expected_generator_identity(
    generator: GeneratorIdentity,
    *,
    expected_code_version: object,
    expected_runner_sha256: object,
    expected_dependency_code_sha256: object,
) -> None:
    if (
        not isinstance(expected_code_version, str)
        or re.fullmatch(r"(?!0{40})[a-f0-9]{40}", expected_code_version) is None
        or not _is_digest(expected_runner_sha256)
        or not _is_digest(expected_dependency_code_sha256)
    ):
        raise CaptureError("CAPTURE_EXPECTED_CODE_IDENTITY_INVALID")
    if (
        generator.git_head != expected_code_version
        or generator.runner_sha256 != expected_runner_sha256
        or generator.dependency_code_sha256
        != expected_dependency_code_sha256
    ):
        raise CaptureError("CAPTURE_CODE_IDENTITY_MISMATCH")


def _require_source_identity(
    manifest_file: Path,
    *,
    manifest_sha256: str,
    generator: GeneratorIdentity,
) -> None:
    if _sha256(
        _read_private_file(
            manifest_file, _MAX_MANIFEST_BYTES, "CAPTURE_MANIFEST_INVALID"
        )
    ) != manifest_sha256:
        raise CaptureError("CAPTURE_MANIFEST_CHANGED")
    if _load_generator_identity() != generator:
        raise CaptureError("CAPTURE_CODE_CHANGED")

def _require_service_identity(
    repository_root: Path,
    *,
    service_code_version: str,
    generator: GeneratorIdentity,
) -> None:
    if (
        not isinstance(service_code_version, str)
        or re.fullmatch(r"(?!0{40})[a-f0-9]{40}", service_code_version)
        is None
    ):
        raise CaptureError("CAPTURE_SERVICE_IDENTITY_INVALID")
    try:
        _run_git(
            repository_root,
            ["merge-base", "--is-ancestor", service_code_version, generator.git_head],
        )
        changed = _run_git(
            repository_root,
            ["diff", "--name-only", service_code_version, generator.git_head, "--"],
        )
    except CaptureError:
        raise CaptureError("CAPTURE_SERVICE_IDENTITY_INVALID") from None
    changed_paths = {line for line in changed.splitlines() if line}
    if not changed_paths.issubset(_SERVICE_ALLOWED_CHANGE_PATHS):
        raise CaptureError("CAPTURE_SERVICE_CODE_CHANGED")

def _parse_manifest(
    raw: bytes, workspace: Path, repository_root: Path
) -> CaptureManifest:
    value = _parse_json_object(raw, "CAPTURE_MANIFEST_INVALID")
    if set(value) != {
        "schemaVersion",
        "artifactKind",
        "captureId",
        "serviceCodeVersion",
        "expectedCaseCount",
        "caseSelection",
        "endpoint",
        "timeoutMs",
        "externalStatementTransferConfirmed",
        "resultContract",
        "corpusIdentity",
        "embeddingIdentity",
        "indexIdentity",
        "runtimeDeclaration",
        "requestIdentitySetSha256",
        "cases",
    }:
        raise CaptureError("CAPTURE_MANIFEST_INVALID")
    if (
        type(value.get("schemaVersion")) is not int
        or value["schemaVersion"] != SCHEMA_VERSION
        or value.get("artifactKind")
        != "anklang_local_query_only_capture_manifest"
        or value.get("externalStatementTransferConfirmed") is not True
    ):
        raise CaptureError("CAPTURE_MANIFEST_INVALID")
    capture_id = value.get("captureId")
    service_code_version = value.get("serviceCodeVersion")
    if (
        not isinstance(capture_id, str)
        or _CAPTURE_ID_RE.fullmatch(capture_id) is None
        or not isinstance(service_code_version, str)
        or re.fullmatch(r"(?!0{40})[a-f0-9]{40}", service_code_version)
        is None
    ):
        raise CaptureError("CAPTURE_MANIFEST_INVALID")
    endpoint = _validate_endpoint(value.get("endpoint"))
    timeout_ms = _bounded_int(value.get("timeoutMs"), 1_000, 600_000)
    result_contract = _validate_result_contract(value.get("resultContract"))
    corpus_identity = _validate_corpus_identity(value.get("corpusIdentity"))
    embedding_identity = _validate_embedding_identity(
        value.get("embeddingIdentity")
    )
    index_identity = _validate_index_identity(value.get("indexIdentity"))
    runtime_declaration = _validate_runtime_declaration(
        value.get("runtimeDeclaration"),
        embedding_identity=embedding_identity,
        index_identity=index_identity,
    )
    expected_case_count = _bounded_int(
        value.get("expectedCaseCount"), 1, MAX_CASE_COUNT
    )
    case_selection = _validate_case_selection(
        value.get("caseSelection"), expected_case_count
    )
    raw_cases = value.get("cases")
    if (
        not isinstance(raw_cases, list)
        or len(raw_cases) != expected_case_count
    ):
        raise CaptureError("CAPTURE_MANIFEST_INVALID")

    cases: list[RequestBinding] = []
    case_ids: set[str] = set()
    file_names: set[str] = set()
    request_sha256s: set[str] = set()
    request_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict) or set(raw_case) != {
            "caseId",
            "request",
        }:
            raise CaptureError("CAPTURE_MANIFEST_INVALID")
        case_id = raw_case.get("caseId")
        request = raw_case.get("request")
        if (
            not isinstance(case_id, str)
            or _CASE_ID_RE.fullmatch(case_id) is None
            or not isinstance(request, dict)
            or set(request) != {"fileName", "sha256"}
        ):
            raise CaptureError("CAPTURE_MANIFEST_INVALID")
        file_name = request.get("fileName")
        expected_sha256 = request.get("sha256")
        if (
            not isinstance(file_name, str)
            or _SAFE_FILE_NAME_RE.fullmatch(file_name) is None
            or not isinstance(expected_sha256, str)
            or _SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise CaptureError("CAPTURE_MANIFEST_INVALID")
        if (
            case_id in case_ids
            or file_name in file_names
            or expected_sha256 in request_sha256s
        ):
            raise CaptureError("CAPTURE_MANIFEST_DUPLICATE")
        request_path = workspace / file_name
        _require_project_ignored(repository_root, request_path)
        request_bytes = _read_private_file(
            request_path, _MAX_REQUEST_BYTES, "CAPTURE_REQUEST_INVALID"
        )
        if _sha256(request_bytes) != expected_sha256:
            raise CaptureError("CAPTURE_REQUEST_HASH_MISMATCH")
        parsed_request = _parse_and_validate_request(request_bytes)
        request_id = parsed_request["request_id"]
        request_identity = request_id.lower()
        if request_identity in request_ids:
            raise CaptureError("CAPTURE_REQUEST_ID_DUPLICATE")
        case_ids.add(case_id)
        file_names.add(file_name)
        request_sha256s.add(expected_sha256)
        request_ids.add(request_identity)
        cases.append(
            RequestBinding(
                case_id=case_id,
                file_name=file_name,
                expected_sha256=expected_sha256,
                request_id=request_id,
                content_hash=parsed_request["content_hash"],
            )
        )

    request_identity_set_sha256 = _request_identity_set_sha256(cases)
    if value.get("requestIdentitySetSha256") != request_identity_set_sha256:
        raise CaptureError("CAPTURE_REQUEST_IDENTITY_SET_MISMATCH")

    return CaptureManifest(
        capture_id=capture_id,
        service_code_version=service_code_version,
        expected_case_count=expected_case_count,
        case_selection=case_selection,
        endpoint=endpoint,
        base_url_sha256=_base_url_sha256(endpoint),
        timeout_ms=timeout_ms,
        result_contract=result_contract,
        corpus_identity=corpus_identity,
        embedding_identity=embedding_identity,
        index_identity=index_identity,
        runtime_declaration=runtime_declaration,
        runtime_declaration_sha256=_hash_json(runtime_declaration),
        request_identity_set_sha256=request_identity_set_sha256,
        cases=tuple(cases),
    )


def _validate_result_contract(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "apiVersion",
        "topLevelKeys",
    }:
        raise CaptureError("CAPTURE_RESULT_CONTRACT_INVALID")
    keys = value.get("topLevelKeys")
    if (
        value.get("apiVersion") != "2"
        or not isinstance(keys, list)
        or keys != list(RESULT_CONTRACT_KEYS)
    ):
        raise CaptureError("CAPTURE_RESULT_CONTRACT_INVALID")
    return {"apiVersion": "2", "topLevelKeys": list(RESULT_CONTRACT_KEYS)}


def _validate_case_selection(
    value: object, expected_case_count: int
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "policy",
        "population",
        "sampleCount",
    }:
        raise CaptureError("CAPTURE_CASE_SELECTION_INVALID")
    if (
        value.get("policy") != "evenly_spaced_source_ids_v1"
        or _bounded_int(value.get("population"), 156, 156)
        != value.get("population")
        or value.get("sampleCount") != expected_case_count
    ):
        raise CaptureError("CAPTURE_CASE_SELECTION_INVALID")
    return {
        "policy": "evenly_spaced_source_ids_v1",
        "population": 156,
        "sampleCount": expected_case_count,
    }



def _validate_corpus_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "evidenceKind",
        "corpusId",
        "sourceCount",
        "sourceSetSha256",
        "sourceConfirmationSha256",
        "materializationMarkerSha256",
        "approvalSha256",
        "requestSetSha256",
        "requestPreparationMarkerSha256",
    }:
        raise CaptureError("CAPTURE_CORPUS_DECLARATION_INVALID")
    corpus_id = value.get("corpusId")
    source_count = value.get("sourceCount")
    if (
        value.get("evidenceKind") != "reproducible_snapshot"
        or not isinstance(corpus_id, str)
        or not 1 <= len(corpus_id) <= 160
        or corpus_id != corpus_id.strip()
        or not corpus_id.startswith("formal156-")
        or _bounded_int(source_count, 156, 156) != source_count
        or not _is_digest(value.get("sourceSetSha256"))
        or not _is_digest(value.get("sourceConfirmationSha256"))
        or not _is_digest(value.get("materializationMarkerSha256"))
        or not _is_digest(value.get("approvalSha256"))
        or not _is_digest(value.get("requestSetSha256"))
        or not _is_digest(value.get("requestPreparationMarkerSha256"))
    ):
        raise CaptureError("CAPTURE_CORPUS_DECLARATION_INVALID")
    return {
        "evidenceKind": "reproducible_snapshot",
        "corpusId": corpus_id,
        "sourceCount": source_count,
        "sourceSetSha256": value["sourceSetSha256"],
        "sourceConfirmationSha256": value["sourceConfirmationSha256"],
        "materializationMarkerSha256": value["materializationMarkerSha256"],
        "approvalSha256": value["approvalSha256"],
        "requestSetSha256": value["requestSetSha256"],
        "requestPreparationMarkerSha256": value["requestPreparationMarkerSha256"],
    }


def _validate_embedding_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "provider",
        "baseUrlSha256",
        "apiKeyPresent",
        "model",
        "dimensions",
        "configured",
        "configurationSha256",
    }:
        raise CaptureError("CAPTURE_EMBEDDING_IDENTITY_INVALID")
    provider = value.get("provider")
    base_url_sha256 = value.get("baseUrlSha256")
    api_key_present = value.get("apiKeyPresent")
    model = value.get("model")
    dimensions = value.get("dimensions")
    configured = value.get("configured")
    if (
        provider != "dashscope"
        or (base_url_sha256 is not None and not _is_digest(base_url_sha256))
        or not isinstance(api_key_present, bool)
        or not isinstance(model, str)
        or not 1 <= len(model) <= 200
        or model != model.strip()
        or _bounded_int(dimensions, 1, 4096) != dimensions
        or not isinstance(configured, bool)
        or not _is_digest(value.get("configurationSha256"))
        or configured != (api_key_present and base_url_sha256 is not None)
    ):
        raise CaptureError("CAPTURE_EMBEDDING_IDENTITY_INVALID")
    return {
        "provider": "dashscope",
        "baseUrlSha256": base_url_sha256,
        "apiKeyPresent": api_key_present,
        "model": model,
        "dimensions": dimensions,
        "configured": configured,
        "configurationSha256": value["configurationSha256"],
    }


def _validate_index_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "indexId",
        "dbSha256",
        "problemCount",
        "vectorIndexReady",
        "vectorIndexStatus",
        "embeddingModel",
        "embeddingDimensions",
    }:
        raise CaptureError("CAPTURE_INDEX_IDENTITY_INVALID")
    index_id = value.get("indexId")
    status = value.get("vectorIndexStatus")
    ready = value.get("vectorIndexReady")
    if (
        not isinstance(index_id, str)
        or not 1 <= len(index_id) <= 160
        or index_id != index_id.strip()
        or not _is_digest(value.get("dbSha256"))
        or _bounded_int(value.get("problemCount"), 0, 100_000_000)
        != value.get("problemCount")
        or not isinstance(ready, bool)
        or status
        not in {
            "empty",
            "ready",
            "missing_metadata",
            "incomplete_vectors",
            "model_mismatch",
            "dimension_mismatch",
            "invalid_metadata",
            "invalid_vectors",
            "embedding_disabled",
            "store_unavailable",
        }
        or ready != (status == "ready")
        or not isinstance(value.get("embeddingModel"), str)
        or not 1 <= len(value["embeddingModel"]) <= 200
        or value["embeddingModel"] != value["embeddingModel"].strip()
        or _bounded_int(value.get("embeddingDimensions"), 1, 4096)
        != value.get("embeddingDimensions")
    ):
        raise CaptureError("CAPTURE_INDEX_IDENTITY_INVALID")
    return {
        "indexId": index_id,
        "dbSha256": value["dbSha256"],
        "problemCount": value["problemCount"],
        "vectorIndexReady": ready,
        "vectorIndexStatus": status,
        "embeddingModel": value["embeddingModel"],
        "embeddingDimensions": value["embeddingDimensions"],
    }


def _validate_runtime_declaration(
    value: object,
    *,
    embedding_identity: dict[str, Any],
    index_identity: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "backend",
        "searchK",
        "minimumSimilarity",
        "blockThreshold",
        "similarityBlockEnabled",
        "cacheTtlSeconds",
        "llmReviewEnabled",
        "llmModel",
        "llmReviewTopN",
        "llmEndpointSha256",
        "reverseProxy",
        "localEngine",
    }:
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    backend = value.get("backend")
    if backend in HISTORICAL_BACKENDS:
        raise CaptureError("CAPTURE_HISTORICAL_BACKEND")
    if backend != "upstream-v2":
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    search_k = _bounded_int(value.get("searchK"), 1, 20)
    minimum_similarity = _finite_number(
        value.get("minimumSimilarity"), minimum=0.0, maximum=1.0
    )
    block_threshold = _finite_number(
        value.get("blockThreshold"), minimum=0.0, maximum=1.0
    )
    if value.get("similarityBlockEnabled") is not False:
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    if _bounded_int(value.get("cacheTtlSeconds"), 0, 0) != 0:
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    if (
        value.get("llmReviewEnabled") is not False
        or value.get("llmModel") is not None
        or value.get("llmReviewTopN") is not None
        or value.get("llmEndpointSha256") is not None
        or value.get("reverseProxy") is not None
    ):
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    local_engine = value.get("localEngine")
    if not isinstance(local_engine, dict) or set(local_engine) != {
        "mode",
        "vectorTopK",
        "keywordTopK",
        "embeddingConfigured",
        "embeddingModel",
        "embeddingDimensions",
        "embeddingEndpointSha256",
        "indexIdentitySha256",
    }:
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    embedding_configured = local_engine.get("embeddingConfigured")
    if (
        local_engine.get("mode") != "query_only"
        or _bounded_int(local_engine.get("vectorTopK"), 1, 200) != search_k
        or _bounded_int(local_engine.get("keywordTopK"), 0, 0) != 0
        or not isinstance(embedding_configured, bool)
        or embedding_configured != embedding_identity["configured"]
        or local_engine.get("indexIdentitySha256") != index_identity["dbSha256"]
    ):
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    if embedding_configured:
        if (
            not isinstance(local_engine.get("embeddingModel"), str)
            or local_engine["embeddingModel"] != embedding_identity["model"]
            or local_engine.get("embeddingDimensions")
            != embedding_identity["dimensions"]
            or local_engine.get("embeddingEndpointSha256")
            != embedding_identity["baseUrlSha256"]
            or not _is_digest(local_engine.get("embeddingEndpointSha256"))
        ):
            raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    elif not (
        local_engine.get("embeddingModel") is None
        and local_engine.get("embeddingDimensions") is None
        and local_engine.get("embeddingEndpointSha256") is None
    ):
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    return {
        "backend": "upstream-v2",
        "searchK": search_k,
        "minimumSimilarity": minimum_similarity,
        "blockThreshold": block_threshold,
        "similarityBlockEnabled": False,
        "cacheTtlSeconds": 0,
        "llmReviewEnabled": False,
        "llmModel": None,
        "llmReviewTopN": None,
        "llmEndpointSha256": None,
        "reverseProxy": None,
        "localEngine": {
            "mode": "query_only",
            "vectorTopK": search_k,
            "keywordTopK": 0,
            "embeddingConfigured": embedding_configured,
            "embeddingModel": local_engine.get("embeddingModel"),
            "embeddingDimensions": local_engine.get("embeddingDimensions"),
            "embeddingEndpointSha256": local_engine.get("embeddingEndpointSha256"),
            "indexIdentitySha256": index_identity["dbSha256"],
        },
    }


def _parse_and_validate_request(raw: bytes) -> dict[str, Any]:
    value = _parse_json_object(raw, "CAPTURE_REQUEST_INVALID")
    try:
        return parse_request(value, expected_api_version="2")
    except ContractError:
        raise CaptureError("CAPTURE_REQUEST_INVALID") from None


def _read_bound_request(
    workspace: Path, repository_root: Path, binding: RequestBinding
) -> bytes:
    path = workspace / binding.file_name
    _require_project_ignored(repository_root, path)
    raw = _read_private_file(path, _MAX_REQUEST_BYTES, "CAPTURE_REQUEST_INVALID")
    if _sha256(raw) != binding.expected_sha256:
        raise CaptureError("CAPTURE_REQUEST_HASH_MISMATCH")
    parsed = _parse_and_validate_request(raw)
    if (
        parsed["request_id"] != binding.request_id
        or parsed["content_hash"] != binding.content_hash
    ):
        raise CaptureError("CAPTURE_REQUEST_BINDING_CHANGED")
    return raw


def _evaluate_response(
    binding: RequestBinding,
    case_index: int,
    response: HttpCaptureResponse,
    *,
    response_sha256: str | None,
) -> dict[str, Any]:
    response_content_type = _header_evidence(response.content_type)
    response_cache_control = _header_evidence(response.cache_control)
    if response.status != 200:
        return _observation(
            binding,
            case_index,
            status="http_error",
            http_status=response.status,
            response_sha256=response_sha256,
            checked_at=None,
            response_content_type=response_content_type,
            response_cache_control=response_cache_control,
        )
    content_type = (response_content_type or "").split(";", 1)[0].strip().lower()
    cache_directives = {
        part.strip().lower()
        for part in (response_cache_control or "").split(",")
        if part.strip()
    }
    if (
        response.body is None
        or content_type != "application/json"
        or "no-store" not in cache_directives
    ):
        return _observation(
            binding,
            case_index,
            status="response_invalid",
            http_status=200,
            response_sha256=response_sha256,
            checked_at=None,
            response_content_type=response_content_type,
            response_cache_control=response_cache_control,
        )
    try:
        value = _parse_json_object(response.body, "CAPTURE_RESPONSE_INVALID")
        result = validate_v2_result(value)
    except (CaptureError, ContractError):
        return _observation(
            binding,
            case_index,
            status="response_invalid",
            http_status=200,
            response_sha256=response_sha256,
            checked_at=None,
            response_content_type=response_content_type,
            response_cache_control=response_cache_control,
        )
    if result["contentHash"] != binding.content_hash:
        return _observation(
            binding,
            case_index,
            status="response_invalid",
            http_status=200,
            response_sha256=response_sha256,
            checked_at=result["checkedAt"],
            response_content_type=response_content_type,
            response_cache_control=response_cache_control,
        )
    if result["completion"]["status"] != "complete":
        return _observation(
            binding,
            case_index,
            status="response_incomplete",
            http_status=200,
            response_sha256=response_sha256,
            checked_at=result["checkedAt"],
            response_content_type=response_content_type,
            response_cache_control=response_cache_control,
        )
    return _observation(
        binding,
        case_index,
        status="complete",
        http_status=200,
        response_sha256=response_sha256,
        checked_at=result["checkedAt"],
        response_content_type=response_content_type,
        response_cache_control=response_cache_control,
    )


def _response_metadata(
    binding: RequestBinding,
    case_index: int,
    response: HttpCaptureResponse,
    *,
    response_sha256: str | None,
) -> dict[str, Any]:
    status = response.status
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
        or (response.body is None) != (response_sha256 is None)
        or (response_sha256 is not None and not _is_digest(response_sha256))
    ):
        raise CaptureError("CAPTURE_RESPONSE_METADATA_INVALID")
    return {
        "schemaVersion": 1,
        "artifactKind": "anklang_review_flow_http_response_metadata",
        "caseId": binding.case_id,
        "caseIndex": case_index,
        "attempt": 1,
        "httpStatus": status,
        "contentType": _header_evidence(response.content_type),
        "cacheControl": _header_evidence(response.cache_control),
        "bodyPresent": response.body is not None,
        "bodySha256": response_sha256,
    }


def _read_response_metadata(
    directory_fd: int,
    *,
    binding: RequestBinding,
    case_index: int,
) -> dict[str, Any]:
    raw = _read_private_file_at(
        directory_fd,
        _response_metadata_output_name(case_index),
        32 * 1024,
        "CAPTURE_RESPONSE_METADATA_INVALID",
    )
    value = _parse_json_object(raw, "CAPTURE_RESPONSE_METADATA_INVALID")
    if set(value) != {
        "schemaVersion",
        "artifactKind",
        "caseId",
        "caseIndex",
        "attempt",
        "httpStatus",
        "contentType",
        "cacheControl",
        "bodyPresent",
        "bodySha256",
    }:
        raise CaptureError("CAPTURE_RESPONSE_METADATA_INVALID")
    status = value.get("httpStatus")
    body_present = value.get("bodyPresent")
    body_sha256 = value.get("bodySha256")
    try:
        content_type = _header_evidence(value.get("contentType"))
        cache_control = _header_evidence(value.get("cacheControl"))
    except CaptureError:
        raise CaptureError("CAPTURE_RESPONSE_METADATA_INVALID") from None
    if (
        type(value.get("schemaVersion")) is not int
        or value["schemaVersion"] != 1
        or value.get("artifactKind")
        != "anklang_review_flow_http_response_metadata"
        or value.get("caseId") != binding.case_id
        or type(value.get("caseIndex")) is not int
        or value["caseIndex"] != case_index
        or type(value.get("attempt")) is not int
        or value["attempt"] != 1
        or isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
        or not isinstance(body_present, bool)
        or body_present != (body_sha256 is not None)
        or (body_sha256 is not None and not _is_digest(body_sha256))
        or content_type != value.get("contentType")
        or cache_control != value.get("cacheControl")
        or raw != _pretty_json(value)
    ):
        raise CaptureError("CAPTURE_RESPONSE_METADATA_INVALID")
    return value


def _observation(
    binding: RequestBinding,
    case_index: int,
    *,
    status: str,
    http_status: int | None,
    response_sha256: str | None,
    checked_at: str | None,
    response_content_type: str | None = None,
    response_cache_control: str | None = None,
) -> dict[str, Any]:
    if status not in _OBSERVATION_STATUSES:
        raise CaptureError("CAPTURE_INTERNAL_ERROR")
    return {
        "caseId": binding.case_id,
        "caseIndex": case_index,
        "attempt": 1,
        "status": status,
        "contentHash": binding.content_hash,
        "requestId": binding.request_id,
        "requestFileName": _request_output_name(case_index),
        "requestSha256": binding.expected_sha256,
        "responseFileName": (
            _response_output_name(case_index)
            if response_sha256 is not None
            else None
        ),
        "responseSha256": response_sha256,
        "httpStatus": http_status,
        "responseContentType": response_content_type,
        "responseCacheControl": response_cache_control,
        "checkedAt": checked_at,
    }


def _recover_cancelled_observation(
    directory_fd: int,
    *,
    binding: RequestBinding,
    case_index: int,
) -> dict[str, Any]:
    request_name = _request_output_name(case_index)
    response_name = _response_output_name(case_index)
    request_sha256: str | None = None
    response_sha256: str | None = None
    if _name_exists(directory_fd, request_name):
        request_raw = _read_private_file_at(
            directory_fd,
            request_name,
            _MAX_REQUEST_BYTES,
            "CAPTURE_REQUEST_SNAPSHOT_INVALID",
            allow_empty=False,
        )
        request_sha256 = _sha256(request_raw)
        if request_sha256 != binding.expected_sha256:
            raise CaptureError("CAPTURE_REQUEST_SNAPSHOT_MISMATCH")
    if _name_exists(directory_fd, response_name):
        response_raw = _read_private_file_at(
            directory_fd,
            response_name,
            _MAX_RESPONSE_BYTES,
            "CAPTURE_RESPONSE_SNAPSHOT_INVALID",
            allow_empty=True,
        )
        response_sha256 = _sha256(response_raw)
    observation = _observation(
        binding,
        case_index,
        status="cancelled",
        http_status=None,
        response_sha256=response_sha256,
        checked_at=None,
    )
    if request_sha256 is None:
        observation["requestFileName"] = None
        observation["requestSha256"] = None
    return observation


def _build_capture_result(
    *,
    manifest: CaptureManifest,
    manifest_sha256: str,
    bindings: dict[str, Any],
    observations: list[dict[str, Any]],
    run_directory_fd: int,
    created_at: str,
    health_before: HealthEvidence,
    health_after: HealthEvidence,
) -> dict[str, Any]:
    audited: list[dict[str, Any]] = []
    for case_index, binding in enumerate(manifest.cases):
        if case_index >= len(observations):
            audited.append(
                _observation(
                    binding,
                    case_index,
                    status="missing",
                    http_status=None,
                    response_sha256=None,
                    checked_at=None,
                )
            )
            continue
        observation = _validate_observation(
            observations[case_index], binding=binding, case_index=case_index
        )
        request_name = observation["requestFileName"]
        if request_name is None or not _name_exists(run_directory_fd, request_name):
            observation = _observation(
                binding,
                case_index,
                status="missing",
                http_status=observation["httpStatus"],
                response_sha256=None,
                checked_at=None,
            )
            observation["requestFileName"] = None
            observation["requestSha256"] = None
            audited.append(observation)
            continue
        request_raw = _read_private_file_at(
            run_directory_fd,
            request_name,
            _MAX_REQUEST_BYTES,
            "CAPTURE_REQUEST_SNAPSHOT_INVALID",
            allow_empty=False,
        )
        if _sha256(request_raw) != binding.expected_sha256:
            raise CaptureError("CAPTURE_REQUEST_SNAPSHOT_MISMATCH")
        response_name = observation["responseFileName"]
        response_sha256 = observation["responseSha256"]
        response_raw: bytes | None = None
        if response_name is not None:
            if not _name_exists(run_directory_fd, response_name):
                observation = dict(observation)
                observation.update(
                    {
                        "status": "missing",
                        "responseFileName": None,
                        "responseSha256": None,
                        "responseContentType": None,
                        "responseCacheControl": None,
                        "checkedAt": None,
                    }
                )
                audited.append(observation)
                continue
            else:
                response_raw = _read_private_file_at(
                    run_directory_fd,
                    response_name,
                    _MAX_RESPONSE_BYTES,
                    "CAPTURE_RESPONSE_SNAPSHOT_INVALID",
                    allow_empty=True,
                )
                if _sha256(response_raw) != response_sha256:
                    raise CaptureError("CAPTURE_RESPONSE_SNAPSHOT_MISMATCH")
        metadata_name = _response_metadata_output_name(case_index)
        if observation["httpStatus"] is not None:
            if not _name_exists(run_directory_fd, metadata_name):
                observation = _observation(
                    binding,
                    case_index,
                    status="missing",
                    http_status=None,
                    response_sha256=None,
                    checked_at=None,
                )
                audited.append(observation)
                continue
            metadata = _read_response_metadata(
                run_directory_fd,
                binding=binding,
                case_index=case_index,
            )
            if (
                metadata["bodySha256"] != response_sha256
                or metadata["bodyPresent"] != (response_raw is not None)
            ):
                raise CaptureError("CAPTURE_RESPONSE_METADATA_MISMATCH")
            derived = _evaluate_response(
                binding,
                case_index,
                HttpCaptureResponse(
                    status=metadata["httpStatus"],
                    content_type=metadata["contentType"],
                    cache_control=metadata["cacheControl"],
                    body=response_raw,
                ),
                response_sha256=response_sha256,
            )
            if derived != observation:
                raise CaptureError("CAPTURE_CHECKPOINT_EVIDENCE_MISMATCH")
        elif _name_exists(run_directory_fd, metadata_name) and observation[
            "status"
        ] != "cancelled":
            raise CaptureError("CAPTURE_CHECKPOINT_EVIDENCE_MISMATCH")
        audited.append(observation)

    complete_count = sum(entry["status"] == "complete" for entry in audited)
    status_counts = {
        status: sum(entry["status"] == status for entry in audited)
        for status in sorted(_OBSERVATION_STATUSES)
    }
    safe_counts = {
        "expected": manifest.expected_case_count,
        "observed": len(audited),
        "complete": complete_count,
        "incomplete": manifest.expected_case_count - complete_count,
        "byStatus": status_counts,
    }
    if complete_count != manifest.expected_case_count:
        return {
            "complete": False,
            "captureId": manifest.capture_id,
            "counts": safe_counts,
        }

    response_hashes = [entry["responseSha256"] for entry in audited]
    if len(set(response_hashes)) != manifest.expected_case_count:
        raise CaptureError("CAPTURE_RESPONSE_HASH_DUPLICATE")
    capture_cases = [
        {
            "caseId": entry["caseId"],
            "requestId": entry["requestId"],
            "requestSha256": entry["requestSha256"],
            "responseSha256": entry["responseSha256"],
            "httpStatus": 200,
            "attempt": 1,
            "responseCompletionStatus": "complete",
        }
        for entry in audited
    ]
    counts = {
        "caseCount": manifest.expected_case_count,
        "requestCount": manifest.expected_case_count,
        "responseCount": manifest.expected_case_count,
        "http200Count": manifest.expected_case_count,
        "attemptCount": manifest.expected_case_count,
        "completeResponseCount": manifest.expected_case_count,
        "failureCount": 0,
    }
    without_fingerprint = {
        "schemaVersion": SCHEMA_VERSION,
        "artifactKind": "anklang_review_flow_capture_attestation",
        "protocolVersion": "anklang-review-flow-capture-v1",
        "captureStatus": "complete",
        "captureId": manifest.capture_id,
        "capturedAt": _validate_existing_time(created_at),
        "capturer": bindings["generator"],
        "serviceCodeVersion": manifest.service_code_version,
        "caseSelection": manifest.case_selection,
        "configuration": {
            "apiVersion": "2",
            "endpointPath": "/api/v2/checks/similarity",
            "baseUrlSha256": manifest.base_url_sha256,
            "timeoutMs": manifest.timeout_ms,
            "authentication": "bearer_redacted",
            "secretsExcluded": True,
        },
        "resultContract": manifest.result_contract,
        "backend": {
            "kind": manifest.runtime_declaration["backend"],
            "mode": manifest.runtime_declaration["localEngine"]["mode"],
            "configurationSha256": _backend_configuration_sha256(
                manifest,
                manifest_sha256,
                health_before,
                health_after,
            ),
            "secretsExcluded": True,
        },
        "embedding": manifest.embedding_identity,
        "index": manifest.index_identity,
        "corpus": manifest.corpus_identity,
        "requestIdentitySetSha256": manifest.request_identity_set_sha256,
        "cases": capture_cases,
        "counts": counts,
    }
    attestation = {
        **without_fingerprint,
        "captureFingerprint": _hash_canonical_value(without_fingerprint),
    }
    return {
        "complete": True,
        "captureId": manifest.capture_id,
        "counts": safe_counts,
        "attestation": attestation,
    }


def _validate_existing_attestation(
    raw: bytes,
    *,
    manifest: CaptureManifest,
    manifest_sha256: str,
    bindings: dict[str, Any],
    observations: list[dict[str, Any]],
    run_directory_fd: int,
    health_before: HealthEvidence,
    health_after: HealthEvidence,
) -> dict[str, Any]:
    value = _parse_json_object(raw, "CAPTURE_ATTESTATION_INVALID")
    if not isinstance(value.get("capturedAt"), str):
        raise CaptureError("CAPTURE_ATTESTATION_INVALID")
    rebuilt = _build_capture_result(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        bindings=bindings,
        observations=observations,
        run_directory_fd=run_directory_fd,
        created_at=value["capturedAt"],
        health_before=health_before,
        health_after=health_after,
    )
    if (
        not rebuilt["complete"]
        or raw != _pretty_json(rebuilt["attestation"])
    ):
        raise CaptureError("CAPTURE_ATTESTATION_INVALID")
    return rebuilt["attestation"]


def _publish_completion_marker(
    directory_fd: int,
    *,
    manifest: CaptureManifest,
    attestation_bytes: bytes,
) -> None:
    attestation = _parse_json_object(
        attestation_bytes, "CAPTURE_ATTESTATION_INVALID"
    )
    marker = _completion_marker_value(
        manifest=manifest,
        attestation_bytes=attestation_bytes,
        attestation=attestation,
    )
    _write_bytes_once(
        directory_fd,
        COMPLETION_MARKER_NAME,
        _pretty_json(marker),
    )


def _completion_marker_value(
    *,
    manifest: CaptureManifest,
    attestation_bytes: bytes,
    attestation: dict[str, Any],
) -> dict[str, Any]:
    cases = attestation.get("cases")
    if (
        not isinstance(cases, list)
        or len(cases) != manifest.expected_case_count
    ):
        raise CaptureError("CAPTURE_ATTESTATION_INVALID")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "artifactKind": "anklang_review_flow_capture_completion",
        "protocolVersion": "anklang-review-flow-capture-v1",
        "captureId": manifest.capture_id,
        "attestationSha256": _sha256(attestation_bytes),
        "captureSetSha256": _capture_set_sha256(cases),
        "caseCount": manifest.expected_case_count,
        "requestCount": manifest.expected_case_count,
        "responseCount": manifest.expected_case_count,
        "http200Count": manifest.expected_case_count,
        "attemptCount": manifest.expected_case_count,
        "completeResponseCount": manifest.expected_case_count,
        "failureCount": 0,
        "complete": True,
    }


def _validate_observation(
    value: object, *, binding: RequestBinding, case_index: int
) -> dict[str, Any]:
    expected_keys = {
        "caseId",
        "caseIndex",
        "attempt",
        "status",
        "contentHash",
        "requestId",
        "requestFileName",
        "requestSha256",
        "responseFileName",
        "responseSha256",
        "httpStatus",
        "responseContentType",
        "responseCacheControl",
        "checkedAt",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    if (
        value.get("caseId") != binding.case_id
        or type(value.get("caseIndex")) is not int
        or value["caseIndex"] != case_index
        or type(value.get("attempt")) is not int
        or value["attempt"] != 1
        or value.get("status") not in _OBSERVATION_STATUSES
        or value.get("contentHash") != binding.content_hash
        or value.get("requestId") != binding.request_id
    ):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    request_file = value.get("requestFileName")
    request_sha = value.get("requestSha256")
    if request_file is not None and request_file != _request_output_name(case_index):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    if request_sha is not None and request_sha != binding.expected_sha256:
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    response_file = value.get("responseFileName")
    response_sha = value.get("responseSha256")
    if (response_file is None) != (response_sha is None):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    if response_file is not None and (
        response_file != _response_output_name(case_index)
        or not _is_digest(response_sha)
    ):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    http_status = value.get("httpStatus")
    if http_status is not None and (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 100 <= http_status <= 599
    ):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    response_content_type = value.get("responseContentType")
    response_cache_control = value.get("responseCacheControl")
    try:
        normalized_content_type = _header_evidence(response_content_type)
        normalized_cache_control = _header_evidence(response_cache_control)
    except CaptureError:
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID") from None
    if (
        normalized_content_type != response_content_type
        or normalized_cache_control != response_cache_control
        or (
            http_status is None
            and (
                response_content_type is not None
                or response_cache_control is not None
            )
        )
    ):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    checked_at = value.get("checkedAt")
    if checked_at is not None and (
        not isinstance(checked_at, str)
        or len(checked_at) > 40
        or not checked_at.endswith("Z")
    ):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    status = value["status"]
    if status == "missing":
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    if status in {"complete", "response_incomplete", "response_invalid"} and (
        http_status != 200
    ):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    if status == "http_error" and (http_status is None or http_status == 200):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    if status in {"transport_error", "cancelled"} and http_status is not None:
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    if status in {"complete", "response_incomplete"} and (
        response_file is None or checked_at is None
    ):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    return dict(value)


def _new_checkpoint(capture_id: str, bindings: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "artifactKind": "anklang_review_flow_capture_checkpoint",
        "captureId": capture_id,
        "bindings": bindings,
        "active": None,
        "invalidated": False,
        "observations": [],
    }


def _load_checkpoint(
    directory_fd: int,
    *,
    manifest: CaptureManifest,
    bindings: dict[str, Any],
) -> dict[str, Any]:
    raw = _read_private_file_at(
        directory_fd,
        CHECKPOINT_NAME,
        _MAX_CHECKPOINT_BYTES,
        "CAPTURE_CHECKPOINT_INVALID",
    )
    value = _parse_json_object(raw, "CAPTURE_CHECKPOINT_INVALID")
    if not isinstance(value, dict) or set(value) != {
        "schemaVersion",
        "artifactKind",
        "captureId",
        "bindings",
        "active",
        "invalidated",
        "observations",
    }:
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    if (
        type(value.get("schemaVersion")) is not int
        or value["schemaVersion"] != SCHEMA_VERSION
        or value.get("artifactKind")
        != "anklang_review_flow_capture_checkpoint"
        or value.get("captureId") != manifest.capture_id
        or value.get("bindings") != bindings
        or not isinstance(value.get("invalidated"), bool)
        or value["invalidated"]
        or not isinstance(value.get("observations"), list)
    ):
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    observations = value["observations"]
    if len(observations) > manifest.expected_case_count:
        raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    normalized: list[dict[str, Any]] = []
    for case_index, observation in enumerate(observations):
        normalized.append(
            _validate_observation(
                observation,
                binding=manifest.cases[case_index],
                case_index=case_index,
            )
        )
    active = value.get("active")
    if active is not None:
        next_index = len(normalized)
        if (
            not isinstance(active, dict)
            or set(active)
            != {"caseId", "caseIndex", "attempt", "requestSha256"}
            or next_index >= manifest.expected_case_count
            or active.get("caseId") != manifest.cases[next_index].case_id
            or type(active.get("caseIndex")) is not int
            or active["caseIndex"] != next_index
            or type(active.get("attempt")) is not int
            or active["attempt"] != 1
            or active.get("requestSha256")
            != manifest.cases[next_index].expected_sha256
        ):
            raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    value["observations"] = normalized
    return value


def _load_generator_identity() -> GeneratorIdentity:
    root = _repository_root()
    status = _run_git_bytes(
        root, ["status", "--porcelain=v1", "--untracked-files=all"]
    )
    if status != b"":
        raise CaptureError("CAPTURE_GIT_WORKTREE_DIRTY")
    head = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    if re.fullmatch(r"[a-f0-9]{40}", head) is None:
        raise CaptureError("CAPTURE_GIT_IDENTITY_INVALID")
    if _run_git_bytes(root, ["ls-files", "private"]) != b"":
        raise CaptureError("CAPTURE_PRIVATE_PATH_TRACKED")
    for option in ("-v", "-f"):
        entries = _run_git_bytes(root, ["ls-files", option, "-z"]).split(b"\0")
        if any(entry and not entry.startswith(b"H ") for entry in entries):
            raise CaptureError("CAPTURE_GIT_INDEX_FLAGS_UNSAFE")

    working_files: list[tuple[str, bytes]] = []
    head_files: list[tuple[str, bytes]] = []
    for relative in FIXED_DEPENDENCY_PATHS:
        tracked = _run_git(root, ["ls-files", "--error-unmatch", "--", relative])
        if tracked != relative:
            raise CaptureError("CAPTURE_DEPENDENCY_UNTRACKED")
        working = _read_dependency_bytes(root, relative)
        committed = _run_git_bytes(root, ["show", f"HEAD:{relative}"])
        if working != committed:
            raise CaptureError("CAPTURE_DEPENDENCY_CHANGED")
        working_files.append((relative, working))
        head_files.append((relative, committed))
    working_bundle = _hash_dependency_files(working_files)
    if working_bundle != _hash_dependency_files(head_files):
        raise CaptureError("CAPTURE_DEPENDENCY_CHANGED")
    runner_bytes = dict(working_files)[FIXED_DEPENDENCY_PATHS[0]]
    return GeneratorIdentity(
        git_head=head,
        runner_sha256=_sha256(runner_bytes),
        dependency_code_sha256=working_bundle,
    )


def _compute_dependency_code_sha256(root: Path) -> str:
    return _hash_dependency_files(
        [
            (relative, _read_dependency_bytes(root, relative))
            for relative in FIXED_DEPENDENCY_PATHS
        ]
    )


def _hash_dependency_files(files: list[tuple[str, bytes]]) -> str:
    # 必须逐字节匹配 Fermata hashEvaluationCodeBundle；清单固定在模块常量，
    # manifest 和 attestation 都无权自报或扩充依赖。
    digest = hashlib.sha256()
    for relative, raw in sorted(files, key=lambda entry: entry[0]):
        name = relative.encode("utf-8")
        digest.update(str(len(name)).encode("ascii"))
        digest.update(b":")
        digest.update(name)
        digest.update(b"\0")
        digest.update(str(len(raw)).encode("ascii"))
        digest.update(b":")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _read_dependency_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    try:
        metadata = path.lstat()
    except OSError:
        raise CaptureError("CAPTURE_DEPENDENCY_INVALID") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CaptureError("CAPTURE_DEPENDENCY_INVALID")
    try:
        return path.read_bytes()
    except OSError:
        raise CaptureError("CAPTURE_DEPENDENCY_INVALID") from None


def _run_git(root: Path, arguments: list[str]) -> str:
    raw = _run_git_bytes(root, arguments)
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise CaptureError("CAPTURE_GIT_IDENTITY_INVALID") from None


def _run_git_bytes(root: Path, arguments: list[str]) -> bytes:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.excludesFile=/dev/null",
                "-c",
                "core.fsmonitor=false",
                *arguments,
            ],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        raise CaptureError("CAPTURE_GIT_IDENTITY_INVALID") from None
    if completed.returncode != 0:
        raise CaptureError("CAPTURE_GIT_IDENTITY_INVALID")
    return completed.stdout


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _require_project_ignored(repository_root: Path, path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(repository_root)
    except ValueError:
        raise CaptureError("CAPTURE_INPUT_NOT_PROJECT_PRIVATE") from None
    try:
        completed = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.excludesFile=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "check-ignore",
                "--quiet",
                "--",
                relative.as_posix(),
            ],
            cwd=repository_root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        raise CaptureError("CAPTURE_INPUT_NOT_PROJECT_PRIVATE") from None
    if completed.returncode != 0:
        raise CaptureError("CAPTURE_INPUT_NOT_PROJECT_PRIVATE")


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("GIT_"):
            environment.pop(key, None)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _validate_service_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 16
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CaptureError("CAPTURE_SERVICE_TOKEN_INVALID")
    return value


def _validate_endpoint(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise CaptureError("CAPTURE_ENDPOINT_INVALID")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise CaptureError("CAPTURE_ENDPOINT_INVALID") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/api/v2/checks/similarity"
        or parsed.query
        or parsed.fragment
    ):
        raise CaptureError("CAPTURE_ENDPOINT_INVALID")
    return value


def _read_or_capture_health_snapshot(
    directory_fd: int,
    snapshot_name: str,
    *,
    endpoint: str,
    service_token: str,
    timeout_seconds: float,
    expected_backend: str,
    allow_existing: bool,
    transport: HealthTransport,
) -> HealthEvidence:
    active_name = f"{snapshot_name}.active"
    snapshot_exists = _name_exists(directory_fd, snapshot_name)
    active_exists = _name_exists(directory_fd, active_name)
    if snapshot_exists:
        if not allow_existing or not active_exists:
            raise CaptureError("CAPTURE_HEALTH_OUTPUT_EXISTS")
        return _read_health_snapshot(
            directory_fd, snapshot_name, expected_backend=expected_backend
        )
    if active_exists:
        # 上一次调用是否已经到达服务端无法判断；显式恢复也不能自动重发。
        raise CaptureError("CAPTURE_HEALTH_UNCERTAIN")
    _write_bytes_once(
        directory_fd,
        active_name,
        _pretty_json({"schemaVersion": 1, "attempt": 1}),
    )
    health_endpoint = _health_endpoint(endpoint)
    try:
        response = transport(health_endpoint, service_token, timeout_seconds)
    except Exception:
        raise CaptureError("CAPTURE_HEALTH_UNAVAILABLE") from None
    body_sha256: str | None = None
    if response.body is not None:
        _write_bytes_once(directory_fd, snapshot_name, response.body)
        body_sha256 = _sha256(response.body)
    metadata = _health_response_metadata(response, body_sha256=body_sha256)
    _write_bytes_once(
        directory_fd,
        _health_metadata_name(snapshot_name),
        _pretty_json(metadata),
    )
    return _read_health_snapshot(
        directory_fd, snapshot_name, expected_backend=expected_backend
    )


def _read_health_snapshot(
    directory_fd: int, snapshot_name: str, *, expected_backend: str
) -> HealthEvidence:
    active_name = f"{snapshot_name}.active"
    if not _name_exists(directory_fd, active_name):
        raise CaptureError("CAPTURE_HEALTH_INVALID")
    raw = _read_private_file_at(
        directory_fd,
        snapshot_name,
        _MAX_HEALTH_BYTES,
        "CAPTURE_HEALTH_INVALID",
    )
    metadata_name = _health_metadata_name(snapshot_name)
    if not _name_exists(directory_fd, metadata_name):
        raise CaptureError("CAPTURE_HEALTH_INVALID")
    metadata_raw = _read_private_file_at(
        directory_fd,
        metadata_name,
        32 * 1024,
        "CAPTURE_HEALTH_INVALID",
    )
    metadata = _parse_json_object(metadata_raw, "CAPTURE_HEALTH_INVALID")
    if set(metadata) != {
        "schemaVersion",
        "artifactKind",
        "attempt",
        "httpStatus",
        "contentType",
        "cacheControl",
        "bodyPresent",
        "bodySha256",
    }:
        raise CaptureError("CAPTURE_HEALTH_INVALID")
    try:
        content_type = _header_evidence(metadata.get("contentType"))
        cache_control = _header_evidence(metadata.get("cacheControl"))
    except CaptureError:
        raise CaptureError("CAPTURE_HEALTH_INVALID") from None
    if (
        type(metadata.get("schemaVersion")) is not int
        or metadata["schemaVersion"] != 1
        or metadata.get("artifactKind")
        != "anklang_review_flow_health_response_metadata"
        or type(metadata.get("attempt")) is not int
        or metadata["attempt"] != 1
        or type(metadata.get("httpStatus")) is not int
        or metadata["httpStatus"] != 200
        or content_type != metadata.get("contentType")
        or cache_control != metadata.get("cacheControl")
        or not isinstance(metadata.get("bodyPresent"), bool)
        or metadata["bodyPresent"] is not True
        or metadata.get("bodySha256") != _sha256(raw)
        or metadata_raw != _pretty_json(metadata)
        or (content_type or "").split(";", 1)[0].strip().lower()
        != "application/json"
        or "no-store" not in _cache_directives(cache_control)
    ):
        raise CaptureError("CAPTURE_HEALTH_INVALID")
    return _validate_health_bytes(
        raw,
        expected_backend,
        metadata_sha256=_sha256(metadata_raw),
    )


def _health_response_metadata(
    response: HttpCaptureResponse, *, body_sha256: str | None
) -> dict[str, Any]:
    status = response.status
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
        or (response.body is None) != (body_sha256 is None)
        or (body_sha256 is not None and not _is_digest(body_sha256))
    ):
        raise CaptureError("CAPTURE_HEALTH_INVALID")
    return {
        "schemaVersion": 1,
        "artifactKind": "anklang_review_flow_health_response_metadata",
        "attempt": 1,
        "httpStatus": status,
        "contentType": _header_evidence(response.content_type),
        "cacheControl": _header_evidence(response.cache_control),
        "bodyPresent": response.body is not None,
        "bodySha256": body_sha256,
    }


def _validate_health_bytes(
    raw: bytes, expected_backend: str, *, metadata_sha256: str
) -> HealthEvidence:
    value = _parse_json_object(raw, "CAPTURE_HEALTH_INVALID")
    if (
        value.get("status") != "ok"
        or value.get("service") != "anklang"
        or value.get("apiVersion") != "1"
        or value.get("backend") != expected_backend
    ):
        raise CaptureError("CAPTURE_HEALTH_INVALID")
    return HealthEvidence(
        sha256=_sha256(raw),
        canonical_sha256=_hash_canonical_value(value),
        metadata_sha256=metadata_sha256,
        backend=expected_backend,
        status="ok",
    )


def _health_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}/api/v1/health"


def _health_metadata_name(snapshot_name: str) -> str:
    return f"{snapshot_name}.meta"


def _backend_configuration_sha256(
    manifest: CaptureManifest,
    manifest_sha256: str,
    health_before: HealthEvidence,
    health_after: HealthEvidence,
) -> str:
    return _hash_canonical_value(
        {
            "protocol": "anklang-review-flow-backend-configuration-v1",
            "declarationSource": "operator_manifest_unverified",
            "runtimeDeclaration": manifest.runtime_declaration,
            "runtimeDeclarationSha256": manifest.runtime_declaration_sha256,
            "captureInput": {
                "manifestSha256": manifest_sha256,
            },
            "health": {
                "beforeSha256": health_before.sha256,
                "afterSha256": health_after.sha256,
                "beforeCanonicalSha256": health_before.canonical_sha256,
                "afterCanonicalSha256": health_after.canonical_sha256,
                "beforeMetadataSha256": health_before.metadata_sha256,
                "afterMetadataSha256": health_after.metadata_sha256,
                "beforeBackend": health_before.backend,
                "afterBackend": health_after.backend,
                "beforeStatus": health_before.status,
                "afterStatus": health_after.status,
                "backendEqual": health_before.backend == health_after.backend,
                "statusEqual": health_before.status == health_after.status,
                "responseEqual": (
                    health_before.canonical_sha256
                    == health_after.canonical_sha256
                ),
            },
        }
    )


def _require_health_equal(
    before: HealthEvidence, after: HealthEvidence
) -> None:
    if (
        before.backend != after.backend
        or before.status != after.status
        or before.canonical_sha256 != after.canonical_sha256
    ):
        raise CaptureError("CAPTURE_HEALTH_CHANGED")


def _header_evidence(value: object) -> str | None:
    if value is None:
        return None
    try:
        encoded_length = len(value.encode("utf-8")) if isinstance(value, str) else 0
    except UnicodeEncodeError:
        raise CaptureError("CAPTURE_RESPONSE_METADATA_INVALID") from None
    if (
        not isinstance(value, str)
        or encoded_length > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CaptureError("CAPTURE_RESPONSE_METADATA_INVALID")
    return value


def _cache_directives(value: str | None) -> set[str]:
    return {
        part.strip().lower()
        for part in (value or "").split(",")
        if part.strip()
    }


def _get_health_once(
    endpoint: str, token: str, timeout_seconds: float
) -> HttpCaptureResponse:
    request = urllib.request.Request(
        endpoint,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Urmotiv-API-Version": "2",
            "User-Agent": "Anklang-Review-Flow-Capture/1",
        },
    )
    return _open_once(request, timeout_seconds)


def _post_once(
    endpoint: str, token: str, request_body: bytes, timeout_seconds: float
) -> HttpCaptureResponse:
    request = urllib.request.Request(
        endpoint,
        data=request_body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Urmotiv-API-Version": "2",
            "User-Agent": "Anklang-Review-Flow-Capture/1",
        },
    )
    return _open_once(request, timeout_seconds)


def _open_once(
    request: urllib.request.Request, timeout_seconds: float
) -> HttpCaptureResponse:
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = _read_response_body(response)
            return HttpCaptureResponse(
                status=int(response.status),
                content_type=response.headers.get("Content-Type"),
                cache_control=response.headers.get("Cache-Control"),
                body=body,
            )
    except urllib.error.HTTPError as error:
        try:
            body = _read_response_body(error)
        except Exception:
            body = None
        return HttpCaptureResponse(
            status=int(error.code),
            content_type=error.headers.get("Content-Type") if error.headers else None,
            cache_control=(
                error.headers.get("Cache-Control") if error.headers else None
            ),
            body=body,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _read_response_body(response: Any) -> bytes | None:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        return None
    return raw


def _prepare_run_directory(root: Path, run_id: str, *, resume: bool) -> Path:
    runs = root / "runs"
    _create_or_validate_private_directory(runs, "CAPTURE_RUNS_DIRECTORY_INVALID")
    run_directory = runs / run_id
    if resume:
        _require_private_directory(run_directory, "CAPTURE_RUN_DIRECTORY_INVALID")
    else:
        try:
            run_directory.mkdir(mode=0o700)
        except FileExistsError:
            raise CaptureError("CAPTURE_RUN_ALREADY_EXISTS") from None
        except OSError:
            raise CaptureError("CAPTURE_RUN_DIRECTORY_INVALID") from None
        _require_private_directory(run_directory, "CAPTURE_RUN_DIRECTORY_INVALID")
    return run_directory


def _require_existing_run_directory(root: Path, run_id: str) -> Path:
    runs = _require_private_directory(
        root / "runs", "CAPTURE_RUNS_DIRECTORY_INVALID"
    )
    return _require_private_directory(
        runs / run_id, "CAPTURE_RUN_DIRECTORY_INVALID"
    )


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
        except OSError:
            raise CaptureError("CAPTURE_RUN_DIRECTORY_CHANGED") from None
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or current.st_dev != self.device
            or current.st_ino != self.inode
        ):
            raise CaptureError("CAPTURE_RUN_DIRECTORY_CHANGED")


def _acquire_run_lock(path: Path) -> _RunDirectoryLock:
    try:
        before = path.lstat()
    except OSError:
        raise CaptureError("CAPTURE_RUN_DIRECTORY_INVALID") from None
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        raise CaptureError("CAPTURE_RUN_DIRECTORY_INVALID") from None
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
            raise CaptureError("CAPTURE_RUN_DIRECTORY_INVALID")
        lock_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        lock_fd = os.open("run.lock", lock_flags, 0o600, dir_fd=directory_fd)
        lock_metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise CaptureError("CAPTURE_RUN_LOCK_INVALID")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise CaptureError("CAPTURE_RUN_LOCKED") from None
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


def _acquire_existing_run_lock(path: Path) -> _RunDirectoryLock:
    try:
        before = path.lstat()
    except OSError:
        raise CaptureError("CAPTURE_RUN_DIRECTORY_INVALID") from None
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        raise CaptureError("CAPTURE_RUN_DIRECTORY_INVALID") from None
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
            raise CaptureError("CAPTURE_RUN_DIRECTORY_INVALID")
        lock_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        lock_fd = os.open("run.lock", lock_flags, dir_fd=directory_fd)
        lock_metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise CaptureError("CAPTURE_RUN_LOCK_INVALID")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise CaptureError("CAPTURE_RUN_LOCKED") from None
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


def _require_private_directory(path: Path, error_code: str) -> Path:
    absolute = Path(os.path.abspath(path))
    _reject_symlink_components(absolute, error_code)
    try:
        metadata = absolute.lstat()
    except OSError:
        raise CaptureError(error_code) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CaptureError(error_code)
    return absolute


def _create_or_validate_private_directory(path: Path, error_code: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _require_private_directory(path, error_code)
    except OSError:
        raise CaptureError(error_code) from None
    else:
        _require_private_directory(path, error_code)


def _require_workspace_input(root: Path, path: Path, error_code: str) -> Path:
    if not path.is_absolute():
        path = Path.cwd() / path
    absolute = Path(os.path.abspath(path))
    _reject_symlink_components(absolute.parent, error_code)
    if absolute.parent != root:
        raise CaptureError(error_code)
    return absolute


def _reject_symlink_components(path: Path, error_code: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            raise CaptureError(error_code) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise CaptureError(error_code)


def _read_private_file(
    path: Path, maximum_bytes: int, error_code: str, *, allow_empty: bool = False
) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        raise CaptureError(error_code) from None
    mode = stat.S_IMODE(before.st_mode)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or mode not in {0o400, 0o600}
    ):
        raise CaptureError(error_code)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CaptureError(error_code) from None
    try:
        return _read_checked_descriptor(
            descriptor,
            before=before,
            maximum_bytes=maximum_bytes,
            error_code=error_code,
            allow_empty=allow_empty,
        )
    finally:
        os.close(descriptor)


def _read_private_file_at(
    directory_fd: int,
    name: str,
    maximum_bytes: int,
    error_code: str,
    *,
    allow_empty: bool = False,
) -> bytes:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        raise CaptureError(error_code) from None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        raise CaptureError(error_code) from None
    try:
        return _read_checked_descriptor(
            descriptor,
            before=before,
            maximum_bytes=maximum_bytes,
            error_code=error_code,
            allow_empty=allow_empty,
        )
    finally:
        os.close(descriptor)


def _read_checked_descriptor(
    descriptor: int,
    *,
    before: os.stat_result,
    maximum_bytes: int,
    error_code: str,
    allow_empty: bool,
) -> bytes:
    opened = os.fstat(descriptor)
    mode = stat.S_IMODE(opened.st_mode)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_dev != before.st_dev
        or opened.st_ino != before.st_ino
        or opened.st_uid != os.geteuid()
        or mode not in {0o400, 0o600}
        or opened.st_size > maximum_bytes
        or (opened.st_size == 0 and not allow_empty)
    ):
        raise CaptureError(error_code)
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    final = os.fstat(descriptor)
    if (
        len(raw) > maximum_bytes
        or final.st_size != len(raw)
        or final.st_mtime_ns != opened.st_mtime_ns
        or (not raw and not allow_empty)
    ):
        raise CaptureError(error_code)
    return raw


def _write_bytes_once(directory_fd: int, name: str, payload: bytes) -> None:
    if (
        _SAFE_FILE_NAME_RE.fullmatch(name) is None
        and _SAFE_TEMP_NAME_RE.fullmatch(name) is None
    ):
        raise CaptureError("CAPTURE_OUTPUT_NAME_INVALID")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError:
        raise CaptureError("CAPTURE_OUTPUT_EXISTS") from None
    except OSError:
        raise CaptureError("CAPTURE_OUTPUT_WRITE_FAILED") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _write_json_replacing(
    directory_fd: int, name: str, value: dict[str, Any]
) -> None:
    if _name_exists(directory_fd, name):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CaptureError("CAPTURE_CHECKPOINT_INVALID")
    temporary = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    _write_bytes_once(directory_fd, temporary, _canonical_json(value) + b"\n")
    try:
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except OSError:
        raise CaptureError("CAPTURE_CHECKPOINT_WRITE_FAILED") from None
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _name_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise CaptureError("CAPTURE_OUTPUT_INSPECTION_FAILED") from None
    return True


def _parse_json_object(raw: bytes, error_code: str) -> dict[str, Any]:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CaptureError(error_code) from None
    if not isinstance(value, dict):
        raise CaptureError(error_code)
    return value


def _request_output_name(case_index: int) -> str:
    return f"case-{case_index + 1:04d}.request.json"


def _response_output_name(case_index: int) -> str:
    return f"case-{case_index + 1:04d}.response.json"


def _response_metadata_output_name(case_index: int) -> str:
    return f"case-{case_index + 1:04d}.response-meta.json"


def _validate_existing_time(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value
        )
        is None
    ):
        raise CaptureError("CAPTURE_ATTESTATION_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise CaptureError("CAPTURE_ATTESTATION_INVALID") from None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise CaptureError("CAPTURE_ATTESTATION_INVALID")
    if _utc_z(parsed) != value:
        raise CaptureError("CAPTURE_ATTESTATION_INVALID")
    return value


def _utc_z(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureError("CAPTURE_CLOCK_INVALID")
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _finite_number(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptureError("CAPTURE_MANIFEST_INVALID")
    number = float(value)
    if not minimum <= number <= maximum or number != number:
        raise CaptureError("CAPTURE_MANIFEST_INVALID")
    return number


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (
        minimum <= value <= maximum
    ):
        raise CaptureError("CAPTURE_RUNTIME_DECLARATION_INVALID")
    return value


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _hash_json(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _hash_canonical_value(value: Any) -> str:
    """与 Fermata ``hashCanonicalValue`` 对普通 JSON 值使用同一编码。"""

    return _hash_json(value)


def _capture_set_sha256(cases: list[Any]) -> str:
    return _hash_canonical_value(
        {
            "protocol": "anklang-review-flow-capture-set-v1",
            "cases": cases,
        }
    )
def _request_identity_set_sha256(
    cases: Sequence[RequestBinding],
) -> str:
    return _hash_canonical_value(
        [
            {
                "caseId": case.case_id,
                "requestId": case.request_id,
                "contentHash": case.content_hash,
                "requestSha256": case.expected_sha256,
            }
            for case in cases
        ]
    )


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CaptureError("CAPTURE_JSON_INVALID") from None


def _pretty_json(value: Any) -> bytes:
    """与 JavaScript ``JSON.stringify(value, null, 2) + LF`` 字节一致。"""

    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                separators=(",", ": "),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CaptureError("CAPTURE_JSON_INVALID") from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _base_url_sha256(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return _sha256(base_url.encode("utf-8"))
