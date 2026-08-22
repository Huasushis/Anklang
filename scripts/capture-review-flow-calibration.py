#!/usr/bin/env python3
"""人工触发 Fermata 审题流程所需的 Anklang v2 私有采集。"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.pycache_prefix = "/dev/null"

import argparse
import os
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from anklang.review_flow_capture import (  # noqa: E402
    CaptureError,
    run_capture,
    verify_capture,
    verify_manifest,
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "参数不合法。\n")


class _VerifyArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.exit(2, "CAPTURE_ARGUMENTS_INVALID\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="单次采集严格 Anklang v2 complete 响应；不会自动重试。"
    )
    parser.add_argument("--workspace", required=True, help="Git 已忽略的私有工作目录")
    parser.add_argument("--manifest", required=True, help="私有采集 manifest")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="恢复同一 capture；进行中请求会固定取消而不会重发",
    )
    return parser


def _verify_parser() -> argparse.ArgumentParser:
    parser = _VerifyArgumentParser(
        prog="capture-review-flow-calibration.py verify-capture",
        description="只读重放并验证已完成的 Anklang v2 capture。",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--verifier-service-code-version", required=True)
    parser.add_argument("--verifier-code-version", required=True)
    parser.add_argument("--verifier-runner-sha256", required=True)
    parser.add_argument("--verifier-dependency-code-sha256", required=True)
    return parser


def _verify_manifest_parser() -> argparse.ArgumentParser:
    parser = _VerifyArgumentParser(
        prog="capture-review-flow-calibration.py verify-manifest",
        description="只读验证冻结的本地 query-only manifest。",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--verifier-service-code-version", required=True)
    return parser


def _verify_main(argv: list[str]) -> int:
    args = _verify_parser().parse_args(argv)
    try:
        attestation_bytes = verify_capture(
            workspace=args.workspace,
            manifest_path=args.manifest,
            expected_service_code_version=args.verifier_service_code_version,
            expected_code_version=args.verifier_code_version,
            expected_runner_sha256=args.verifier_runner_sha256,
            expected_dependency_code_sha256=(
                args.verifier_dependency_code_sha256
            ),
        )
    except KeyboardInterrupt:
        sys.stderr.write("CAPTURE_CANCELLED\n")
        return 130
    except CaptureError as error:
        sys.stderr.write(f"{error.code}\n")
        return 1
    except Exception:
        sys.stderr.write("CAPTURE_INTERNAL_ERROR\n")
        return 1
    sys.stdout.buffer.write(attestation_bytes)
    sys.stdout.buffer.flush()
    return 0


def _verify_manifest_main(argv: list[str]) -> int:
    args = _verify_manifest_parser().parse_args(argv)
    try:
        summary_bytes = verify_manifest(
            workspace=args.workspace,
            manifest_path=args.manifest,
            expected_service_code_version=args.verifier_service_code_version,
        )
    except CaptureError as error:
        sys.stderr.write(f"{error.code}\n")
        return 1
    except Exception:
        sys.stderr.write("CAPTURE_INTERNAL_ERROR\n")
        return 1
    sys.stdout.buffer.write(summary_bytes)
    sys.stdout.buffer.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "verify-manifest":
        return _verify_manifest_main(arguments[1:])
    if arguments and arguments[0] == "verify-capture":
        return _verify_main(arguments[1:])
    args = _parser().parse_args(arguments)
    try:
        result = run_capture(
            workspace=args.workspace,
            manifest_path=args.manifest,
            service_token=os.environ.get("ANKLANG_SERVICE_TOKEN", ""),
            resume=args.resume,
        )
    except KeyboardInterrupt:
        sys.stderr.write("采集已中断；进行中样本不会自动重发。\n")
        return 130
    except CaptureError as error:
        sys.stderr.write(f"采集失败：{error.code}\n")
        return 1
    except Exception:
        sys.stderr.write("采集失败：CAPTURE_INTERNAL_ERROR\n")
        return 1

    counts = result["counts"]
    sys.stdout.write(
        "采集完成："
        f"complete={'true' if result['complete'] else 'false'}，"
        f"expected={counts['expected']}，"
        f"completeResponses={counts['complete']}，"
        f"incomplete={counts['incomplete']}。\n"
    )
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
