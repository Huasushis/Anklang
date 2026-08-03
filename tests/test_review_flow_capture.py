"""Fermata 审题流程的 Anklang 可信采集测试；只使用合成内容和本地 HTTP。"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import py_compile
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from anklang.contracts import build_v2_result
from anklang import review_flow_capture as capture


_TOKEN = "synthetic-service-token-123456"
_HEALTH_BODY = json.dumps(
    {
        "status": "ok",
        "service": "anklang",
        "apiVersion": "1",
        "backend": "reverse_proxy",
    },
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
_IDENTITY = capture.GeneratorIdentity(
    git_head="1" * 40,
    runner_sha256="2" * 64,
    dependency_code_sha256="3" * 64,
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _complete_response(request: bytes) -> bytes:
    payload = json.loads(request.decode("utf-8"))
    result = build_v2_result(
        payload["contentHash"],
        [],
        False,
        "合成响应。",
        {"status": "complete", "reasonCode": "complete", "retryable": False},
        {"policy": "no-store"},
        checked_at="2026-08-02T00:00:00.000Z",
    )
    return _json_bytes(result)


class _CaptureHttpServer:
    def __init__(
        self,
        *,
        response_mode: str = "complete",
        health_bodies: list[bytes] | None = None,
    ) -> None:
        self.response_mode = response_mode
        self.health_bodies = health_bodies or [_HEALTH_BODY, _HEALTH_BODY]
        self.health_requests: list[dict[str, str]] = []
        self.post_requests: list[dict[str, Any]] = []
        harness = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                harness.health_requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "apiVersion": self.headers.get(
                            "X-Urmotiv-API-Version", ""
                        ),
                    }
                )
                index = len(harness.health_requests) - 1
                body = harness.health_bodies[min(index, len(harness.health_bodies) - 1)]
                self._reply(200, body)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                harness.post_requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "apiVersion": self.headers.get(
                            "X-Urmotiv-API-Version", ""
                        ),
                        "body": body,
                    }
                )
                if harness.response_mode == "http_error":
                    self._reply(503, b'{"error":"synthetic"}')
                    return
                response = _complete_response(body)
                if harness.response_mode == "wrong_hash":
                    value = json.loads(response.decode("utf-8"))
                    value["contentHash"] = "f" * 64
                    response = _json_bytes(value)
                self._reply(200, response)

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v2/checks/similarity"

    def __enter__(self) -> _CaptureHttpServer:
        self.thread.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ReviewFlowCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "private-capture"
        self.workspace.mkdir(mode=0o700)
        self.identity_patch = patch.object(
            capture, "_load_generator_identity", return_value=_IDENTITY
        )
        self.repository_patch = patch.object(
            capture, "_repository_root", return_value=Path(self.temporary.name)
        )
        self.ignore_patch = patch.object(capture, "_require_project_ignored")
        self.identity_patch.start()
        self.repository_patch.start()
        self.ignore_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.addCleanup(self.repository_patch.stop)
        self.addCleanup(self.ignore_patch.stop)

    def _write_private(self, name: str, raw: bytes) -> Path:
        path = self.workspace / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def _prepare_manifest(
        self,
        endpoint: str,
        case_count: int,
        *,
        expected_case_count: int | None = None,
        capture_id: str = "capture-0123456789abcdef",
    ) -> tuple[Path, list[bytes]]:
        requests: list[bytes] = []
        cases: list[dict[str, Any]] = []
        for index in range(case_count):
            request = _json_bytes(
                {
                    "apiVersion": "2",
                    "requestId": f"00000000-0000-4000-8000-{index + 1:012x}",
                    "contentHash": hashlib.sha256(
                        f"synthetic-content-{index}".encode("ascii")
                    ).hexdigest(),
                    "problem": {
                        "title": f"合成题目 {index + 1}",
                        "type": "traditional",
                        "tagIds": ["synthetic"],
                        "basicStatement": f"只用于本地测试的合成题面 {index + 1}。",
                    },
                }
            )
            file_name = f"request-{index + 1:04d}.json"
            self._write_private(file_name, request)
            requests.append(request)
            cases.append(
                {
                    "caseId": f"case-synthetic-{index + 1}",
                    "request": {"fileName": file_name, "sha256": _sha256(request)},
                }
            )
        upstream_hash = "4" * 64
        manifest = {
            "schemaVersion": 1,
            "artifactKind": "anklang_review_flow_v2_capture_manifest",
            "captureId": capture_id,
            "expectedCaseCount": (
                case_count
                if expected_case_count is None
                else expected_case_count
            ),
            "endpoint": endpoint,
            "timeoutMs": 5_000,
            "externalStatementTransferConfirmed": True,
            "runtimeDeclaration": {
                "backend": "reverse_proxy",
                "searchK": 8,
                "minimumSimilarity": 0.5,
                "blockThreshold": 0.9,
                "similarityBlockEnabled": False,
                "cacheTtlSeconds": 3_600,
                "llmReviewEnabled": False,
                "llmModel": None,
                "llmReviewTopN": None,
                "llmEndpointSha256": None,
                "reverseProxy": {
                    "useRerank": False,
                    "upstreamEndpointSha256": upstream_hash,
                },
                "localEngine": None,
                "corpus": {
                    "evidenceKind": "remote_corpus_unverifiable",
                    "serviceOriginSha256": upstream_hash,
                    "declarationSha256": "5" * 64,
                },
            },
            "cases": cases,
        }
        return self._write_private("manifest.json", _json_bytes(manifest)), requests

    def _run(self, manifest: Path, *, resume: bool = False, **kwargs: Any) -> dict[str, Any]:
        with patch.dict(
            os.environ,
            {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
        ):
            return capture.run_capture(
                workspace=self.workspace,
                manifest_path=manifest,
                service_token=_TOKEN,
                resume=resume,
                now=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc),
                **kwargs,
            )

    def _create_complete_capture(self) -> tuple[Path, Path, bytes]:
        manifest, _requests = self._prepare_manifest(
            "http://127.0.0.1:8730/api/v2/checks/similarity", 1
        )

        def health(
            endpoint: str, token: str, timeout: float
        ) -> capture.HttpCaptureResponse:
            del endpoint, token, timeout
            return capture.HttpCaptureResponse(
                status=200,
                content_type="application/json",
                cache_control="no-store",
                body=_HEALTH_BODY,
            )

        def post(
            endpoint: str, token: str, request: bytes, timeout: float
        ) -> capture.HttpCaptureResponse:
            del endpoint, token, timeout
            return capture.HttpCaptureResponse(
                status=200,
                content_type="application/json",
                cache_control="no-store",
                body=_complete_response(request),
            )

        result = self._run(
            manifest,
            transport=post,
            health_transport=health,
        )
        self.assertTrue(result["complete"])
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        return manifest, run, (run / capture.ATTESTATION_NAME).read_bytes()

    def _verify(self, manifest: Path) -> bytes:
        return capture.verify_capture(
            workspace=self.workspace,
            manifest_path=manifest,
            expected_code_version=_IDENTITY.git_head,
            expected_runner_sha256=_IDENTITY.runner_sha256,
            expected_dependency_code_sha256=(
                _IDENTITY.dependency_code_sha256
            ),
        )

    def _assert_complete_capture(
        self,
        server: _CaptureHttpServer,
        result: dict[str, Any],
        requests: list[bytes],
    ) -> None:
        self.assertTrue(result["complete"])
        self.assertEqual(len(server.health_requests), 2)
        self.assertEqual(len(server.post_requests), len(requests))
        self.assertEqual(
            [entry["body"] for entry in server.post_requests], requests
        )
        for request in server.health_requests + server.post_requests:
            self.assertEqual(request["authorization"], f"Bearer {_TOKEN}")
            self.assertEqual(request["apiVersion"], "2")
        self.assertTrue(
            all(
                entry["path"] == "/api/v2/checks/similarity"
                for entry in server.post_requests
            )
        )
        self.assertTrue(
            all(
                entry["path"] == "/api/v1/health"
                for entry in server.health_requests
            )
        )

        run = self.workspace / "runs" / "capture-0123456789abcdef"
        for phase in ("before", "after"):
            health_path = run / f"health-{phase}.response.json"
            metadata_path = run / f"health-{phase}.response.json.meta"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(health_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(metadata_path.stat().st_mode), 0o600)
            self.assertEqual(metadata["httpStatus"], 200)
            self.assertEqual(metadata["cacheControl"], "no-store")
            self.assertEqual(metadata["bodySha256"], _sha256(health_path.read_bytes()))
        attestation_path = run / capture.ATTESTATION_NAME
        marker_path = run / capture.COMPLETION_MARKER_NAME
        self.assertEqual(stat.S_IMODE(attestation_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o600)
        for index, expected in enumerate(requests, start=1):
            snapshot = run / f"case-{index:04d}.request.json"
            self.assertEqual(snapshot.read_bytes(), expected)
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
            response = run / f"case-{index:04d}.response.json"
            metadata_path = run / f"case-{index:04d}.response-meta.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(response.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(metadata_path.stat().st_mode), 0o600)
            self.assertEqual(metadata["httpStatus"], 200)
            self.assertEqual(metadata["contentType"], "application/json")
            self.assertEqual(metadata["cacheControl"], "no-store")
            self.assertEqual(metadata["bodySha256"], _sha256(response.read_bytes()))

        attestation_raw = attestation_path.read_bytes()
        attestation = json.loads(attestation_raw.decode("utf-8"))
        self.assertEqual(
            attestation_raw,
            json.dumps(attestation, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n",
        )
        without_fingerprint = dict(attestation)
        fingerprint = without_fingerprint.pop("captureFingerprint")
        self.assertEqual(
            fingerprint, capture._hash_canonical_value(without_fingerprint)
        )
        self.assertEqual(attestation["counts"]["attemptCount"], len(requests))
        self.assertEqual(attestation["backend"]["kind"], "reverse_proxy")
        self.assertEqual(
            attestation["corpus"]["evidenceKind"],
            "remote_corpus_unverifiable",
        )

        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["attestationSha256"], _sha256(attestation_raw))
        self.assertEqual(
            marker["captureSetSha256"],
            capture._capture_set_sha256(attestation["cases"]),
        )
        self.assertEqual(marker["attemptCount"], len(requests))
        self.assertTrue(marker["complete"])

    def test_one_case_uses_exact_raw_request_once(self) -> None:
        with _CaptureHttpServer() as server:
            manifest, requests = self._prepare_manifest(server.endpoint, 1)
            result = self._run(manifest)
        self._assert_complete_capture(server, result, requests)

    def test_verify_capture_is_offline_read_only_and_returns_raw_attestation(self) -> None:
        manifest, run, attestation_bytes = self._create_complete_capture()

        def state() -> dict[str, tuple[int, int, str]]:
            return {
                path.relative_to(self.workspace).as_posix(): (
                    stat.S_IMODE(path.stat().st_mode),
                    path.stat().st_mtime_ns,
                    _sha256(path.read_bytes()),
                )
                for path in sorted(self.workspace.rglob("*"))
                if path.is_file()
            }

        before = state()
        with patch.object(
            capture.urllib.request.OpenerDirector,
            "open",
            side_effect=AssertionError("verify must not use HTTP"),
        ):
            verified = self._verify(manifest)
        self.assertEqual(verified, attestation_bytes)
        self.assertEqual(state(), before)
        self.assertTrue((run / capture.COMPLETION_MARKER_NAME).is_file())

    def test_verify_rejects_marker_tampering(self) -> None:
        manifest, run, _attestation = self._create_complete_capture()
        marker = run / capture.COMPLETION_MARKER_NAME
        marker.write_bytes(b'{"complete":true}\n')
        marker.chmod(0o600)
        with self.assertRaises(capture.CaptureError) as raised:
            self._verify(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_COMPLETION_INVALID")

    def test_verify_rejects_health_tampering(self) -> None:
        manifest, run, _attestation = self._create_complete_capture()
        health_path = run / capture.HEALTH_AFTER_NAME
        value = json.loads(health_path.read_text(encoding="utf-8"))
        value["upstreamProblemCount"] = 11
        health_path.write_bytes(_json_bytes(value))
        health_path.chmod(0o600)
        metadata_path = run / f"{capture.HEALTH_AFTER_NAME}.meta"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["bodySha256"] = _sha256(health_path.read_bytes())
        metadata_path.write_bytes(
            json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        metadata_path.chmod(0o600)
        with self.assertRaises(capture.CaptureError) as raised:
            self._verify(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_HEALTH_CHANGED")

    def test_verify_rejects_response_metadata_tampering(self) -> None:
        manifest, run, _attestation = self._create_complete_capture()
        metadata_path = run / "case-0001.response-meta.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["cacheControl"] = "public"
        metadata_path.write_bytes(
            json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        metadata_path.chmod(0o600)
        with self.assertRaises(capture.CaptureError) as raised:
            self._verify(manifest)
        self.assertEqual(
            raised.exception.code, "CAPTURE_CHECKPOINT_EVIDENCE_MISMATCH"
        )

    def test_verify_rejects_checkpoint_tampering(self) -> None:
        manifest, run, _attestation = self._create_complete_capture()
        checkpoint_path = run / capture.CHECKPOINT_NAME
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["observations"][0]["status"] = "response_invalid"
        checkpoint_path.write_bytes(_json_bytes(checkpoint))
        checkpoint_path.chmod(0o600)
        with self.assertRaises(capture.CaptureError) as raised:
            self._verify(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_CHECKPOINT_INCOMPLETE")

    def test_verify_rejects_attestation_tampering(self) -> None:
        manifest, run, _attestation = self._create_complete_capture()
        attestation_path = run / capture.ATTESTATION_NAME
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        attestation["captureFingerprint"] = "0" * 64
        attestation_path.write_bytes(
            json.dumps(attestation, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        attestation_path.chmod(0o600)
        with self.assertRaises(capture.CaptureError) as raised:
            self._verify(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_ATTESTATION_INVALID")

    def test_verify_rejects_manifest_source_tampering(self) -> None:
        manifest, _run, _attestation = self._create_complete_capture()
        value = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_bytes(
            json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        manifest.chmod(0o600)
        with self.assertRaises(capture.CaptureError) as raised:
            self._verify(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_CHECKPOINT_INVALID")

    def test_thirty_six_cases_are_manifest_driven(self) -> None:
        with _CaptureHttpServer() as server:
            manifest, requests = self._prepare_manifest(server.endpoint, 36)
            result = self._run(manifest)
        self._assert_complete_capture(server, result, requests)

    def test_manifest_count_mismatch_calls_no_http(self) -> None:
        with _CaptureHttpServer() as server:
            manifest, _requests = self._prepare_manifest(
                server.endpoint, 1, expected_case_count=2
            )
            with self.assertRaises(capture.CaptureError) as raised:
                self._run(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_MANIFEST_INVALID")
        self.assertEqual(server.health_requests, [])
        self.assertEqual(server.post_requests, [])

    def test_local_engine_declaration_is_rejected_before_http(self) -> None:
        with _CaptureHttpServer() as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["runtimeDeclaration"]["backend"] = "local_engine"
            manifest.write_bytes(_json_bytes(value))
            manifest.chmod(0o600)
            with self.assertRaises(capture.CaptureError) as raised:
                self._run(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_LOCAL_CORPUS_UNVERIFIED")
        self.assertEqual(server.health_requests, [])
        self.assertEqual(server.post_requests, [])

    def test_wrong_content_hash_makes_batch_incomplete_without_marker(self) -> None:
        with _CaptureHttpServer(response_mode="wrong_hash") as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            result = self._run(manifest)
        self.assertFalse(result["complete"])
        self.assertEqual(len(server.post_requests), 1)
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        self.assertFalse((run / capture.ATTESTATION_NAME).exists())
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_resume_publishes_marker_without_repeating_completed_calls(self) -> None:
        with _CaptureHttpServer() as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)

            def stop_before_marker() -> None:
                raise RuntimeError("synthetic stop")

            with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
                self._run(
                    manifest,
                    before_completion_marker=stop_before_marker,
                )
            run = self.workspace / "runs" / "capture-0123456789abcdef"
            self.assertTrue((run / capture.ATTESTATION_NAME).is_file())
            self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())
            post_count = len(server.post_requests)
            health_count = len(server.health_requests)

            result = self._run(manifest, resume=True)

        self.assertTrue(result["complete"])
        self.assertEqual(len(server.post_requests), post_count)
        self.assertEqual(len(server.health_requests), health_count)
        self.assertTrue((run / capture.COMPLETION_MARKER_NAME).is_file())

    def test_tampered_checkpoint_complete_status_is_rederived_and_rejected(self) -> None:
        with _CaptureHttpServer(response_mode="wrong_hash") as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            result = self._run(manifest)
        self.assertFalse(result["complete"])
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        checkpoint_path = run / capture.CHECKPOINT_NAME
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["observations"][0]["status"], "response_invalid")
        checkpoint["observations"][0]["status"] = "complete"
        checkpoint_path.write_bytes(_json_bytes(checkpoint))
        checkpoint_path.chmod(0o600)

        with self.assertRaises(capture.CaptureError) as raised:
            self._run(manifest, resume=True)
        self.assertEqual(
            raised.exception.code, "CAPTURE_CHECKPOINT_EVIDENCE_MISMATCH"
        )
        self.assertFalse((run / capture.ATTESTATION_NAME).exists())
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_tampered_response_snapshot_cannot_complete_resume(self) -> None:
        with _CaptureHttpServer() as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            with self.assertRaises(RuntimeError):
                self._run(
                    manifest,
                    before_completion_marker=lambda: (_ for _ in ()).throw(
                        RuntimeError("synthetic stop")
                    ),
                )
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        response_path = run / "case-0001.response.json"
        response_path.write_bytes(b'{"synthetic":"tampered"}')
        response_path.chmod(0o600)

        with self.assertRaises(capture.CaptureError) as raised:
            self._run(manifest, resume=True)
        self.assertEqual(
            raised.exception.code, "CAPTURE_RESPONSE_SNAPSHOT_MISMATCH"
        )
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_tampered_attestation_cannot_complete_resume(self) -> None:
        with _CaptureHttpServer() as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            with self.assertRaises(RuntimeError):
                self._run(
                    manifest,
                    before_completion_marker=lambda: (_ for _ in ()).throw(
                        RuntimeError("synthetic stop")
                    ),
                )
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        attestation_path = run / capture.ATTESTATION_NAME
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        attestation["captureFingerprint"] = "0" * 64
        attestation_path.write_bytes(
            json.dumps(attestation, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        attestation_path.chmod(0o600)

        with self.assertRaises(capture.CaptureError) as raised:
            self._run(manifest, resume=True)
        self.assertEqual(raised.exception.code, "CAPTURE_ATTESTATION_INVALID")
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_precreated_attestation_cannot_promote_incomplete_checkpoint(self) -> None:
        with _CaptureHttpServer(response_mode="wrong_hash") as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            result = self._run(manifest)
        self.assertFalse(result["complete"])
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        attestation_path = run / capture.ATTESTATION_NAME
        attestation_path.write_bytes(
            _json_bytes({"capturedAt": "2026-08-02T00:00:00.000Z"})
        )
        attestation_path.chmod(0o600)

        with self.assertRaises(capture.CaptureError) as raised:
            self._run(manifest, resume=True)
        self.assertEqual(raised.exception.code, "CAPTURE_ATTESTATION_INVALID")
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_tampered_http_metadata_is_rederived_and_rejected(self) -> None:
        with _CaptureHttpServer() as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            with self.assertRaises(RuntimeError):
                self._run(
                    manifest,
                    before_completion_marker=lambda: (_ for _ in ()).throw(
                        RuntimeError("synthetic stop")
                    ),
                )
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        metadata_path = run / "case-0001.response-meta.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["cacheControl"] = "public"
        metadata_path.write_bytes(
            json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        metadata_path.chmod(0o600)

        with self.assertRaises(capture.CaptureError) as raised:
            self._run(manifest, resume=True)
        self.assertEqual(
            raised.exception.code, "CAPTURE_CHECKPOINT_EVIDENCE_MISMATCH"
        )
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_interrupted_request_is_cancelled_and_never_retried(self) -> None:
        manifest, requests = self._prepare_manifest(
            "http://127.0.0.1:8730/api/v2/checks/similarity", 3
        )
        initial_calls: list[bytes] = []
        health_calls: list[str] = []

        def health(endpoint: str, token: str, timeout: float) -> capture.HttpCaptureResponse:
            del token, timeout
            health_calls.append(endpoint)
            return capture.HttpCaptureResponse(
                status=200,
                content_type="application/json",
                cache_control="no-store",
                body=_HEALTH_BODY,
            )

        def interrupted(
            endpoint: str, token: str, request: bytes, timeout: float
        ) -> capture.HttpCaptureResponse:
            del endpoint, token, timeout
            initial_calls.append(request)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self._run(
                manifest,
                transport=interrupted,
                health_transport=health,
            )
        self.assertEqual(initial_calls, [requests[0]])

        resumed_calls: list[bytes] = []

        def completed(
            endpoint: str, token: str, request: bytes, timeout: float
        ) -> capture.HttpCaptureResponse:
            del endpoint, token, timeout
            resumed_calls.append(request)
            return capture.HttpCaptureResponse(
                status=200,
                content_type="application/json",
                cache_control="no-store",
                body=_complete_response(request),
            )

        result = self._run(
            manifest,
            resume=True,
            transport=completed,
            health_transport=health,
        )
        self.assertFalse(result["complete"])
        self.assertEqual(resumed_calls, requests[1:])
        self.assertEqual(len(health_calls), 2)
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        self.assertFalse((run / capture.ATTESTATION_NAME).exists())
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_second_health_mismatch_prevents_attestation(self) -> None:
        changed = _json_bytes(
            {
                "status": "ok",
                "service": "anklang",
                "apiVersion": "1",
                "backend": "local_engine",
            }
        )
        with _CaptureHttpServer(health_bodies=[_HEALTH_BODY, changed]) as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            with self.assertRaises(capture.CaptureError) as raised:
                self._run(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_HEALTH_INVALID")
        self.assertEqual(len(server.health_requests), 2)
        self.assertEqual(len(server.post_requests), 1)
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        self.assertFalse((run / capture.ATTESTATION_NAME).exists())
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_invalid_health_http_metadata_is_not_accepted_on_resume(self) -> None:
        manifest, _requests = self._prepare_manifest(
            "http://127.0.0.1:8730/api/v2/checks/similarity", 1
        )
        bad_calls = 0

        def bad_health(
            endpoint: str, token: str, timeout: float
        ) -> capture.HttpCaptureResponse:
            nonlocal bad_calls
            del endpoint, token, timeout
            bad_calls += 1
            return capture.HttpCaptureResponse(
                status=503,
                content_type="application/json",
                cache_control="no-store",
                body=_HEALTH_BODY,
            )

        with self.assertRaises(capture.CaptureError) as first:
            self._run(manifest, health_transport=bad_health)
        self.assertEqual(first.exception.code, "CAPTURE_HEALTH_INVALID")
        self.assertEqual(bad_calls, 1)

        retry_calls = 0

        def forbidden_retry(
            endpoint: str, token: str, timeout: float
        ) -> capture.HttpCaptureResponse:
            nonlocal retry_calls
            del endpoint, token, timeout
            retry_calls += 1
            return capture.HttpCaptureResponse(
                status=200,
                content_type="application/json",
                cache_control="no-store",
                body=_HEALTH_BODY,
            )

        with self.assertRaises(capture.CaptureError) as resumed:
            self._run(manifest, resume=True, health_transport=forbidden_retry)
        self.assertEqual(resumed.exception.code, "CAPTURE_HEALTH_INVALID")
        self.assertEqual(retry_calls, 0)
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_health_detail_change_prevents_attestation(self) -> None:
        before = _json_bytes(
            {
                "status": "ok",
                "service": "anklang",
                "apiVersion": "1",
                "backend": "reverse_proxy",
                "upstreamReady": True,
                "upstreamProblemCount": 10,
            }
        )
        after = _json_bytes(
            {
                "status": "ok",
                "service": "anklang",
                "apiVersion": "1",
                "backend": "reverse_proxy",
                "upstreamReady": True,
                "upstreamProblemCount": 11,
            }
        )
        with _CaptureHttpServer(health_bodies=[before, after]) as server:
            manifest, _requests = self._prepare_manifest(server.endpoint, 1)
            with self.assertRaises(capture.CaptureError) as raised:
                self._run(manifest)
        self.assertEqual(raised.exception.code, "CAPTURE_HEALTH_CHANGED")
        run = self.workspace / "runs" / "capture-0123456789abcdef"
        self.assertFalse((run / capture.ATTESTATION_NAME).exists())
        self.assertFalse((run / capture.COMPLETION_MARKER_NAME).exists())

    def test_health_whitespace_difference_is_canonically_equal(self) -> None:
        pretty_health = json.dumps(
            json.loads(_HEALTH_BODY.decode("utf-8")),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        with _CaptureHttpServer(
            health_bodies=[_HEALTH_BODY, pretty_health]
        ) as server:
            manifest, requests = self._prepare_manifest(server.endpoint, 1)
            result = self._run(manifest)
        self._assert_complete_capture(server, result, requests)


class GeneratorIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)
        for relative in capture.FIXED_DEPENDENCY_PATHS:
            path = self.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"synthetic dependency: {relative}\n", encoding="utf-8")
        self._git("init", "--quiet")
        self._git("config", "user.name", "Synthetic Test")
        self._git("config", "user.email", "synthetic@example.invalid")
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "synthetic identity")

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["/usr/bin/git", *arguments],
            cwd=self.repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

    def test_clean_tracked_dependency_identity_is_stable(self) -> None:
        with patch.object(capture, "_repository_root", return_value=self.repository):
            first = capture._load_generator_identity()
            second = capture._load_generator_identity()
        self.assertEqual(first, second)
        self.assertNotEqual(first.git_head, "0" * 40)
        self.assertEqual(
            first.dependency_code_sha256,
            capture._compute_dependency_code_sha256(self.repository),
        )

    def test_assume_unchanged_flag_is_rejected(self) -> None:
        relative = capture.FIXED_DEPENDENCY_PATHS[-1]
        self._git("update-index", "--assume-unchanged", relative)
        with patch.object(capture, "_repository_root", return_value=self.repository):
            with self.assertRaises(capture.CaptureError) as raised:
                capture._load_generator_identity()
        self.assertEqual(raised.exception.code, "CAPTURE_GIT_INDEX_FLAGS_UNSAFE")


class CaptureCliTests(unittest.TestCase):
    def test_argument_error_does_not_echo_private_values(self) -> None:
        private_value = "private/statement-and-token-value"
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/capture-review-flow-calibration.py",
                "--workspace",
                private_value,
                "--manifest",
                private_value,
                "--unexpected",
                private_value,
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(private_value.encode("utf-8"), completed.stdout)
        self.assertNotIn(private_value.encode("utf-8"), completed.stderr)

    def test_verify_cli_writes_only_saved_attestation_bytes(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "capture-review-flow-calibration.py"
        )
        spec = importlib.util.spec_from_file_location("capture_cli_test", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        previous_dont_write_bytecode = sys.dont_write_bytecode
        previous_pycache_prefix = sys.pycache_prefix
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous_dont_write_bytecode
            sys.pycache_prefix = previous_pycache_prefix
        expected = b'{"synthetic":"attestation"}\n'

        class BinaryStdout:
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

        stdout = BinaryStdout()
        with (
            patch.object(module, "verify_capture", return_value=expected),
            patch.object(module.sys, "stdout", stdout),
        ):
            result = module.main(
                [
                    "verify-capture",
                    "--workspace",
                    "private/synthetic",
                    "--manifest",
                    "private/synthetic/manifest.json",
                    "--verifier-code-version",
                    "1" * 40,
                    "--verifier-runner-sha256",
                    "2" * 64,
                    "--verifier-dependency-code-sha256",
                    "3" * 64,
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(stdout.buffer.getvalue(), expected)

    def test_verify_cli_does_not_generate_project_bytecode(self) -> None:
        repository = Path(__file__).resolve().parents[1]

        def project_bytecode_state() -> dict[str, tuple[int, str]]:
            result: dict[str, tuple[int, str]] = {}
            for root in (repository / "anklang", repository / "scripts"):
                for path in root.rglob("*.pyc"):
                    if path.name.startswith(
                        ("review_flow_capture", "contracts", "capture-review-flow")
                    ):
                        result[path.relative_to(repository).as_posix()] = (
                            path.stat().st_mtime_ns,
                            _sha256(path.read_bytes()),
                        )
            return result

        before = project_bytecode_state()
        with tempfile.TemporaryDirectory() as cache_prefix:
            environment = dict(os.environ)
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            environment["PYTHONPYCACHEPREFIX"] = cache_prefix
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/capture-review-flow-calibration.py",
                    "verify-capture",
                    "--workspace",
                    "private/synthetic",
                    "--manifest",
                    "private/synthetic/manifest.json",
                    "--verifier-code-version",
                    "1" * 40,
                    "--verifier-runner-sha256",
                    "2" * 64,
                    "--verifier-dependency-code-sha256",
                    "3" * 64,
                ],
                cwd=repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            generated_project_bytecode = [
                path
                for path in Path(cache_prefix).rglob("*.pyc")
                if path.name.startswith(
                    ("review_flow_capture", "contracts", "capture-review-flow")
                )
            ]
            self.assertEqual(generated_project_bytecode, [])
        self.assertEqual(project_bytecode_state(), before)

    def test_direct_runner_ignores_unchecked_hash_project_pyc(self) -> None:
        source_script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "capture-review-flow-calibration.py"
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "Anklang"
            scripts = repository / "scripts"
            package = repository / "anklang"
            scripts.mkdir(parents=True)
            package.mkdir()
            runner = scripts / "capture-review-flow-calibration.py"
            runner.write_bytes(source_script.read_bytes())
            (package / "__init__.py").write_text("", encoding="utf-8")
            dependency = package / "review_flow_capture.py"
            dependency.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "class CaptureError(Exception):",
                        "    def __init__(self, code: str):",
                        "        self.code = code",
                        "def run_capture(**_kwargs):",
                        "    raise CaptureError('SYNTHETIC_RUN_FORBIDDEN')",
                        "def verify_capture(**kwargs) -> bytes:",
                        "    return (Path(kwargs['workspace']) / 'attestation.json').read_bytes()",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            workspace = Path(directory) / "private-workspace"
            workspace.mkdir()
            expected = b'{"source":"attestation"}\n'
            (workspace / "attestation.json").write_bytes(expected)
            manifest = workspace / "manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            marker = Path(directory) / "malicious.marker"
            malicious_source = Path(directory) / "malicious.py"
            malicious_source.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')",
                        "class CaptureError(Exception):",
                        "    def __init__(self, code: str):",
                        "        self.code = code",
                        "def run_capture(**_kwargs):",
                        "    raise CaptureError('SYNTHETIC_RUN_FORBIDDEN')",
                        "def verify_capture(**kwargs) -> bytes:",
                        "    return (Path(kwargs['workspace']) / 'attestation.json').read_bytes()",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            previous_pycache_prefix = sys.pycache_prefix
            try:
                sys.pycache_prefix = None
                pyc_path = Path(importlib.util.cache_from_source(str(dependency)))
            finally:
                sys.pycache_prefix = previous_pycache_prefix
            pyc_path.parent.mkdir(parents=True)
            py_compile.compile(
                str(malicious_source),
                cfile=str(pyc_path),
                dfile=str(dependency),
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )
            malicious_source.unlink()

            environment = dict(os.environ)
            for key in (
                "PYTHONPYCACHEPREFIX",
                "PYTHONDONTWRITEBYTECODE",
                "PYTHONPATH",
            ):
                environment.pop(key, None)
            control = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "sys.path.insert(0, sys.argv[1]); "
                        "import anklang.review_flow_capture"
                    ),
                    str(repository),
                ],
                cwd=repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(control.returncode, 0, control.stderr)
            self.assertTrue(marker.is_file())
            marker.unlink()

            def project_bytecode_state() -> dict[str, tuple[int, str]]:
                return {
                    path.relative_to(repository).as_posix(): (
                        path.stat().st_mtime_ns,
                        _sha256(path.read_bytes()),
                    )
                    for path in repository.rglob("*.pyc")
                }

            before = project_bytecode_state()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "verify-capture",
                    "--workspace",
                    str(workspace),
                    "--manifest",
                    str(manifest),
                    "--verifier-code-version",
                    "1" * 40,
                    "--verifier-runner-sha256",
                    "2" * 64,
                    "--verifier-dependency-code-sha256",
                    "3" * 64,
                ],
                cwd=repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, expected)
            self.assertEqual(completed.stderr, b"")
            self.assertFalse(marker.exists())
            self.assertEqual(project_bytecode_state(), before)


if __name__ == "__main__":
    unittest.main()
