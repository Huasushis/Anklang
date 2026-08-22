#!/usr/bin/env python3
"""Freeze a private Formal156 local query-only acceptance manifest.

This tool performs no embedding request and never prints request or source text.
It only fingerprints the authorized input sets, creates an empty private SQLite
index, and freezes a deterministic 32-case request binding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.pycache_prefix = "/dev/null"

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from anklang.config import ConfigError, load_config  # noqa: E402
from anklang.contracts import ContractError, parse_request  # noqa: E402
from anklang.review_flow_capture import (  # noqa: E402
    RequestBinding,
    _canonical_json,
    _pretty_json,
    _request_identity_set_sha256,
)
from anklang.store import ProblemStore  # noqa: E402


SERVICE_HEAD_RE = re.compile(r"(?!0{40})[a-f0-9]{40}")
EXPECTED_COUNT = 156
CASE_COUNT = 32


class AcceptanceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze a private Formal156 local query-only manifest."
    )
    parser.add_argument("--pipeline-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--service-head", required=True)
    return parser


def _private_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceError("ACCEPTANCE_PRIVATE_INPUT_INVALID")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise AcceptanceError("ACCEPTANCE_PRIVATE_INPUT_MODE")
    return path


def _private_sha256(path: Path) -> str:
    raw = _private_file(path).read_bytes()
    return hashlib.sha256(raw).hexdigest()


def _direct_files(directory: Path, suffix: str) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise AcceptanceError("ACCEPTANCE_PRIVATE_INPUT_INVALID")
    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    files = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_file() or entry.suffix != suffix:
            raise AcceptanceError("ACCEPTANCE_PRIVATE_INPUT_INVALID")
        files.append(_private_file(entry))
    return files


def _set_sha256(files: list[Path], base: Path) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(base).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
def _authoritative_source_set_sha256(
    report: dict[str, Any],
    marker: dict[str, Any],
    source_files: list[Path],
) -> str:
    raw_sources = report.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != EXPECTED_COUNT:
        raise AcceptanceError("ACCEPTANCE_SOURCE_SET_REPORT_INVALID")

    entries: list[dict[str, Any]] = []
    source_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_sources:
        if not isinstance(item, dict):
            raise AcceptanceError("ACCEPTANCE_SOURCE_SET_REPORT_INVALID")
        source_id = item.get("sourceId")
        source_sha256 = item.get("sourceSha256")
        byte_length = item.get("byteLength")
        if (
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(source_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
            or not isinstance(byte_length, int)
            or isinstance(byte_length, bool)
            or byte_length < 0
            or source_id in source_by_id
        ):
            raise AcceptanceError("ACCEPTANCE_SOURCE_SET_REPORT_INVALID")
        entry = {
            "sourceId": source_id,
            "sourceSha256": source_sha256,
            "byteLength": byte_length,
        }
        entries.append(entry)
        source_by_id[source_id] = entry

    if {path.stem for path in source_files} != set(source_by_id):
        raise AcceptanceError("ACCEPTANCE_SOURCE_SET_FILES_MISMATCH")
    for path in source_files:
        raw = path.read_bytes()
        entry = source_by_id[path.stem]
        if (
            len(raw) != entry["byteLength"]
            or hashlib.sha256(raw).hexdigest() != entry["sourceSha256"]
        ):
            raise AcceptanceError("ACCEPTANCE_SOURCE_SET_FILES_MISMATCH")

    marker_sha256 = marker.get("sourceSetSha256")
    if (
        not isinstance(marker_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", marker_sha256) is None
        or marker.get("sourceCount") != EXPECTED_COUNT
        or marker.get("unresolvedItemCount") != 0
    ):
        raise AcceptanceError("ACCEPTANCE_SOURCE_SET_MARKER_INVALID")
    payload = json.dumps(
        {"version": 1, "sources": entries},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != marker_sha256:
        raise AcceptanceError("ACCEPTANCE_SOURCE_SET_MARKER_MISMATCH")
    return actual_sha256



def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_private_file(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AcceptanceError("ACCEPTANCE_AUTHORIZATION_INVALID") from None
    if not isinstance(value, dict):
        raise AcceptanceError("ACCEPTANCE_AUTHORIZATION_INVALID")
    return value


def _select_indices(population: int, sample_count: int) -> list[int]:
    if population < sample_count or sample_count < 1:
        raise AcceptanceError("ACCEPTANCE_SELECTION_INVALID")
    denominator = sample_count - 1
    return [
        (index * (population - 1) + denominator // 2) // denominator
        for index in range(sample_count)
    ]


def _embedding_identity(config: Any) -> dict[str, Any]:
    base_url_sha256 = (
        hashlib.sha256(config.dashscope_base_url.encode("utf-8")).hexdigest()
        if config.dashscope_base_url is not None
        else None
    )
    api_key_present = config.dashscope_api_key is not None
    identity_without_hash = {
        "provider": "dashscope",
        "baseUrlSha256": base_url_sha256,
        "apiKeyPresent": api_key_present,
        "model": config.dashscope_embedding_model,
        "dimensions": config.dashscope_embedding_dim,
    }
    configured = base_url_sha256 is not None and api_key_present
    return {
        **identity_without_hash,
        "configured": configured,
        "configurationSha256": hashlib.sha256(
            _canonical_json(identity_without_hash)
        ).hexdigest(),
    }


def _load_selected_requests(
    request_files: list[Path], indices: list[int], output: Path
) -> tuple[list[dict[str, Any]], list[RequestBinding]]:
    cases: list[dict[str, Any]] = []
    bindings: list[RequestBinding] = []
    for ordinal, source_index in enumerate(indices, start=1):
        input_path = request_files[source_index]
        try:
            raw = input_path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
            normalized = parse_request(value, expected_api_version="2")
        except (OSError, UnicodeError, json.JSONDecodeError, ContractError):
            raise AcceptanceError("ACCEPTANCE_REQUEST_INVALID") from None
        output_name = f"request-{ordinal:04d}.json"
        output_path = output / output_name
        output_path.write_bytes(raw)
        output_path.chmod(0o600)
        request_sha256 = hashlib.sha256(raw).hexdigest()
        binding = RequestBinding(
            case_id=f"case-source-{ordinal:03d}",
            file_name=output_name,
            expected_sha256=request_sha256,
            request_id=normalized["request_id"],
            content_hash=normalized["content_hash"],
        )
        bindings.append(binding)
        cases.append(
            {
                "caseId": binding.case_id,
                "request": {
                    "fileName": binding.file_name,
                    "sha256": binding.expected_sha256,
                },
            }
        )
    return cases, bindings


def _write_private_json(path: Path, value: object) -> bytes:
    raw = _pretty_json(value)
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    if SERVICE_HEAD_RE.fullmatch(args.service_head) is None:
        raise AcceptanceError("ACCEPTANCE_SERVICE_HEAD_INVALID")
    pipeline_root = Path(args.pipeline_root).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        raise AcceptanceError("ACCEPTANCE_OUTPUT_EXISTS")
    output.mkdir(mode=0o700, parents=True)
    output.chmod(0o700)

    materialized = pipeline_root / "materialized-156"
    prepared = pipeline_root / "prepared-156-formal-v2"
    source_files = _direct_files(materialized / "sources", ".md")
    request_files = _direct_files(prepared / "requests", ".json")
    if len(source_files) != EXPECTED_COUNT or len(request_files) != EXPECTED_COUNT:
        raise AcceptanceError("ACCEPTANCE_CORPUS_COUNT_MISMATCH")
    source_confirmation = materialized / "source-confirmation.private.json"
    approval = pipeline_root / "approval-156-formal.private.json"
    materialization_marker_path = materialized / "MATERIALIZE_COMPLETE"
    materialization_report_path = materialized / "report.json"
    preparation_marker = prepared / "PREPARE_COMPLETE"
    _json_object(source_confirmation)
    _json_object(approval)
    materialization_report = _json_object(materialization_report_path)
    materialization_marker = _json_object(materialization_marker_path)

    source_set_sha256 = _authoritative_source_set_sha256(
        materialization_report, materialization_marker, source_files
    )
    request_set_sha256 = _set_sha256(request_files, prepared / "requests")
    indices = _select_indices(EXPECTED_COUNT, CASE_COUNT)

    try:
        config = load_config()
    except ConfigError:
        raise AcceptanceError("ACCEPTANCE_CONFIG_INVALID") from None
    embedding = _embedding_identity(config)
    if not embedding["configured"]:
        raise AcceptanceError("ACCEPTANCE_EMBEDDING_NOT_CONFIGURED")

    index_path = output / "local-index.db"
    store = ProblemStore(str(index_path))
    try:
        inspection = store.inspect_index(None)
    finally:
        store.close()
    index_path.chmod(0o600)
    if (
        inspection.problem_count <= 0
        or not inspection.vector_ready
        or inspection.status != "ready"
    ):
        raise AcceptanceError("ACCEPTANCE_INDEX_NOT_READY")
    index_db_sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest()
    index = {
        "indexId": "formal156-local-index-v1",
        "dbSha256": index_db_sha256,
        "problemCount": inspection.problem_count,
        "vectorIndexReady": inspection.vector_ready,
        "vectorIndexStatus": inspection.status,
        "embeddingModel": config.dashscope_embedding_model,
        "embeddingDimensions": config.dashscope_embedding_dim,
    }

    cases, bindings = _load_selected_requests(request_files, indices, output)
    manifest = {
        "schemaVersion": 2,
        "artifactKind": "anklang_local_query_only_capture_manifest",
        "captureId": "capture-2026082200000000",
        "serviceCodeVersion": args.service_head,
        "expectedCaseCount": CASE_COUNT,
        "caseSelection": {
            "policy": "evenly_spaced_source_ids_v1",
            "population": EXPECTED_COUNT,
            "sampleCount": CASE_COUNT,
        },
        "endpoint": f"http://127.0.0.1:{config.port}/api/v2/checks/similarity",
        "timeoutMs": 600_000,
        "externalStatementTransferConfirmed": True,
        "resultContract": {
            "apiVersion": "2",
            "topLevelKeys": [
                "apiVersion",
                "contentHash",
                "checkedAt",
                "completion",
                "candidates",
            ],
        },
        "corpusIdentity": {
            "evidenceKind": "reproducible_snapshot",
            "corpusId": "formal156-authorized-v1",
            "sourceCount": EXPECTED_COUNT,
            "sourceSetSha256": source_set_sha256,
            "sourceConfirmationSha256": _private_sha256(source_confirmation),
            "materializationMarkerSha256": _private_sha256(materialization_marker_path),
            "approvalSha256": _private_sha256(approval),
            "requestSetSha256": request_set_sha256,
            "requestPreparationMarkerSha256": _private_sha256(preparation_marker),
        },
        "embeddingIdentity": embedding,
        "indexIdentity": index,
        "runtimeDeclaration": {
            "backend": "upstream-v2",
            "searchK": config.search_k,
            "minimumSimilarity": config.minimum_similarity,
            "blockThreshold": 0.0,
            "similarityBlockEnabled": False,
            "cacheTtlSeconds": 0,
            "llmReviewEnabled": False,
            "llmModel": None,
            "llmReviewTopN": None,
            "llmEndpointSha256": None,
            "reverseProxy": None,
            "localEngine": {
                "mode": "query_only",
                "vectorTopK": config.search_k,
                "keywordTopK": 0,
                "embeddingConfigured": embedding["configured"],
                "embeddingModel": (
                    embedding["model"] if embedding["configured"] else None
                ),
                "embeddingDimensions": (
                    embedding["dimensions"] if embedding["configured"] else None
                ),
                "embeddingEndpointSha256": (
                    embedding["baseUrlSha256"] if embedding["configured"] else None
                ),
                "indexIdentitySha256": index_db_sha256,
            },
        },
        "requestIdentitySetSha256": _request_identity_set_sha256(bindings),
        "cases": cases,
    }
    manifest_path = output / "manifest.json"
    manifest_raw = _write_private_json(manifest_path, manifest)
    _write_private_json(
        output / "MANIFEST_FROZEN",
        {
            "manifestSha256": hashlib.sha256(manifest_raw).hexdigest(),
            "caseCount": CASE_COUNT,
            "serviceCodeVersion": args.service_head,
        },
    )
    return {
        "complete": True,
        "manifestSha256": hashlib.sha256(manifest_raw).hexdigest(),
        "caseCount": CASE_COUNT,
        "sourceCount": EXPECTED_COUNT,
        "sourceSetSha256": source_set_sha256,
        "requestSetSha256": request_set_sha256,
        "requestIdentitySetSha256": manifest["requestIdentitySetSha256"],
        "serviceCodeVersion": args.service_head,
        "embeddingProvider": embedding["provider"],
        "embeddingModel": embedding["model"],
        "embeddingDimensions": embedding["dimensions"],
        "embeddingConfigured": embedding["configured"],
        "indexProblemCount": inspection.problem_count,
        "indexStatus": inspection.status,
        "indexDbSha256": index_db_sha256,
        "externalEmbeddingCalls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    try:
        result = _prepare(_parser().parse_args(argv))
    except AcceptanceError as error:
        sys.stderr.write(f"{error.code}\n")
        return 1
    sys.stdout.buffer.write(_pretty_json(result))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
