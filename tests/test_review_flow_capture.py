from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from anklang import review_flow_capture as capture


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _complete_response(request: bytes) -> bytes:
    from anklang.contracts import build_v2_result

    payload = json.loads(request.decode("utf-8"))
    result = build_v2_result(
        content_hash=payload["contentHash"],
        candidates=[],
        completion={
            "status": "complete",
            "reasonCode": "complete",
            "retryable": False,
        },
        checked_at="2026-08-02T00:00:00.000Z",
    )
    return _json_bytes(result)


def _health_body() -> bytes:
    return _json_bytes(
        {
            "status": "ok",
            "service": "anklang",
            "apiVersion": "1",
            "backend": "reverse_proxy",
        }
    )


class ReviewFlowCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "private-capture"
        self.workspace.mkdir(mode=0o700)
        self.identity = capture.GeneratorIdentity(
            git_head="1" * 40,
            runner_sha256="2" * 64,
            dependency_code_sha256="3" * 64,
        )
        self.identity_patch = patch.object(
            capture, "_load_generator_identity", return_value=self.identity
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

    def _prepare_manifest(self, case_count: int) -> tuple[Path, str]:
        requests: list[bytes] = []
        cases: list[dict[str, object]] = []
        for index in range(case_count):
            request = _json_bytes(
                {
                    "apiVersion": "2",
                    "requestId": f"00000000-0000-4000-8000-{index + 1:012x}",
                    "contentHash": _sha256(
                        f"synthetic-{index}".encode("ascii")
                    ),
                    "problem": {
                        "title": f"合成 {index + 1}",
                        "type": "traditional",
                        "tagIds": ["synthetic"],
                        "basicStatement": "本地测试合成题面，无真实内容。",
                    },
                }
            )
            file_name = f"request-{index + 1:04d}.json"
            path = self.workspace / file_name
            path.write_bytes(request)
            path.chmod(0o600)
            requests.append(request)
            cases.append(
                {
                    "caseId": f"case-synthetic-{index + 1}",
                    "request": {"fileName": file_name, "sha256": _sha256(request)},
                }
            )
        upstream_hash = "4" * 64
        capture_id = "capture-0123456789abcdef"
        manifest = {
            "schemaVersion": 1,
            "artifactKind": "anklang_review_flow_v2_capture_manifest",
            "captureId": capture_id,
            "expectedCaseCount": case_count,
            "endpoint": "http://127.0.0.1:1/api/v2/checks/similarity",
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
        manifest_path = self.workspace / "manifest.json"
        manifest_path.write_bytes(_json_bytes(manifest))
        manifest_path.chmod(0o600)
        return manifest_path, capture_id

    def _run(
        self, manifest: Path, case_count: int
    ) -> dict[str, object]:
        from anklang.review_flow_capture import HttpCaptureResponse

        def health(
            endpoint: str, token: str, timeout: float
        ) -> HttpCaptureResponse:
            return HttpCaptureResponse(
                status=200,
                content_type="application/json",
                cache_control="no-store",
                body=_health_body(),
            )

        def post(
            endpoint: str, token: str, request: bytes, timeout: float
        ) -> HttpCaptureResponse:
            return HttpCaptureResponse(
                status=200,
                content_type="application/json",
                cache_control="no-store",
                body=_complete_response(request),
            )

        with patch.dict(
            os.environ,
            {
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            },
        ):
            return capture.run_capture(
                workspace=self.workspace,
                manifest_path=manifest,
                service_token="0" * 16,
                resume=False,
                now=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc),
                transport=post,
                health_transport=health,
            )

    def test_accounting_and_fingerprint(self) -> None:
        manifest, capture_id = self._prepare_manifest(3)
        result = self._run(manifest, 3)
        self.assertTrue(result["complete"], result)
        counts = result["counts"]
        self.assertEqual(counts["expected"], 3)
        self.assertEqual(counts["observed"], 3)
        self.assertEqual(counts["complete"], 3)
        self.assertEqual(counts["incomplete"], 0)
        self.assertEqual(counts["byStatus"]["cancelled"], 0)
        self.assertEqual(counts["byStatus"]["missing"], 0)
        self.assertEqual(counts["byStatus"]["http_error"], 0)
        self.assertEqual(counts["byStatus"]["response_invalid"], 0)
        self.assertEqual(counts["byStatus"]["response_incomplete"], 0)
        self.assertEqual(counts["byStatus"]["transport_error"], 0)
        run = self.workspace / "runs" / capture_id
        attestation_raw = (run / capture.ATTESTATION_NAME).read_bytes()
        marker_raw = (run / capture.COMPLETION_MARKER_NAME).read_bytes()
        attestation = json.loads(attestation_raw.decode("utf-8"))
        marker = json.loads(marker_raw.decode("utf-8"))
        acounts = attestation["counts"]
        self.assertEqual(acounts["caseCount"], 3)
        self.assertEqual(acounts["requestCount"], 3)
        self.assertEqual(acounts["responseCount"], 3)
        self.assertEqual(acounts["http200Count"], 3)
        self.assertEqual(acounts["attemptCount"], 3)
        self.assertEqual(acounts["completeResponseCount"], 3)
        self.assertEqual(acounts["failureCount"], 0)
        self.assertEqual(marker["attestationSha256"], _sha256(attestation_raw))
        self.assertEqual(len(attestation["captureFingerprint"]), 64)
        # 隐私：响应字节与请求字节不相等（服务不原样回显题面）
        for index in range(1, 4):
            request_raw = (
                run / f"case-{index:04d}.request.json"
            ).read_bytes()
            response_raw = (
                run / f"case-{index:04d}.response.json"
            ).read_bytes()
            self.assertNotEqual(request_raw, response_raw)

    def test_offline_verify_reproduces_fingerprint(self) -> None:
        manifest, capture_id = self._prepare_manifest(2)
        result = self._run(manifest, 2)
        self.assertTrue(result["complete"])
        run = self.workspace / "runs" / capture_id
        attestation_raw = (run / capture.ATTESTATION_NAME).read_bytes()
        identity = self.identity
        verified = capture.verify_capture(
            workspace=self.workspace,
            manifest_path=manifest,
            expected_code_version=identity.git_head,
            expected_runner_sha256=identity.runner_sha256,
            expected_dependency_code_sha256=identity.dependency_code_sha256,
        )
        self.assertEqual(verified, attestation_raw)
        # 私有文件权限
        for fname in ("manifest.json", "request-0001.json", "request-0002.json"):
            p = self.workspace / fname
            self.assertEqual(oct(stat.S_IMODE(p.stat().st_mode)), "0o600")
        self.assertEqual(
            oct(stat.S_IMODE(self.workspace.stat().st_mode)), "0o700"
        )
        self.assertEqual(oct(stat.S_IMODE(run.stat().st_mode)), "0o700")


if __name__ == "__main__":
    unittest.main()
