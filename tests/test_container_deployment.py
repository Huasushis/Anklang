"""容器清单的静态安全约束；不启动 Docker，也不访问网络。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath


_ROOT = Path(__file__).resolve().parents[1]


class ContainerDeploymentTests(unittest.TestCase):
    def test_image_is_minimal_non_root_and_copies_only_runtime_files(self) -> None:
        dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.11-slim", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", dockerfile)
        self.assertIn("COPY --chown=10001:10001 anklang /app/anklang", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^\s*(?:COPY|ADD)\s+\.\s")
        self.assertNotRegex(
            dockerfile,
            r"(?mi)^\s*(?:COPY|ADD)\s+.*(?:\.env|private|problems-data|cache|report|\.git)",
        )
        self.assertIn("/api/v1/live", dockerfile)

    def test_build_context_is_deny_by_default(self) -> None:
        dockerignore = (_ROOT / ".dockerignore").read_text(encoding="utf-8")
        rules = [
            line.strip()
            for line in dockerignore.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            rules,
            [
                "**",
                "!anklang/",
                "!anklang/*.py",
                "!anklang/backends/",
                "!anklang/backends/*.py",
                "!anklang/sources/",
                "!anklang/sources/*.py",
                "!anklang/sources/example_static/",
                "!anklang/sources/example_static/__init__.py",
                "!anklang/sources/example_static/problems.json",
                "!LICENSE",
            ],
        )

        def is_included(path: str, *, directory: bool = False) -> bool:
            candidate = f"{path.rstrip('/')}" + ("/" if directory else "")
            included = True
            for rule in rules:
                negated = rule.startswith("!")
                pattern = rule[1:] if negated else rule
                if PurePosixPath(candidate).match(pattern):
                    included = negated
            return included

        for allowed, directory in (
            ("LICENSE", False),
            ("anklang", True),
            ("anklang/server.py", False),
            ("anklang/backends", True),
            ("anklang/backends/__init__.py", False),
            ("anklang/backends/local_engine.py", False),
            ("anklang/sources", True),
            ("anklang/sources/__init__.py", False),
            ("anklang/sources/example_static", True),
            ("anklang/sources/example_static/__init__.py", False),
            ("anklang/sources/example_static/problems.json", False),
        ):
            with self.subTest(allowed=allowed):
                self.assertTrue(is_included(allowed, directory=directory))

        for forbidden in (
            "anklang/__pycache__/server.cpython-312.pyc",
            "anklang/generated.pyc",
            "anklang/.env",
            "anklang/nested/.env.local.py",
            "anklang/local-index.db",
            "anklang/private/problem.md",
            "anklang/private/problem.py",
            "anklang/nested/private/problem.py",
            "anklang/one/two/private/problem.py",
            "anklang/.git/config.py",
            "anklang/tests/test.py",
            "anklang/credentials/key.py",
            "anklang/backends/private/problem.py",
            "anklang/backends/nested/helper.py",
            "anklang/sources/private/problem.py",
            "anklang/sources/example_static/extra.py",
            "anklang/nested/__pycache__/generated.py",
            "anklang/nested/secrets/token.py",
            "anklang/nested/problems-data/problem.py",
            "anklang/nested/cache/generated.py",
            "anklang/nested/reports/result.py",
            "anklang/nested/logs/runtime.py",
            "anklang/runtime.log",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(is_included(forbidden))

    def test_compose_keeps_host_private_and_container_locked_down(self) -> None:
        compose = (_ROOT / "compose.yaml").read_text(encoding="utf-8")
        required_fragments = (
            'user: "10001:10001"',
            "read_only: true",
            "cap_drop:\n      - ALL",
            "no-new-privileges:true",
            '"127.0.0.1:${ANKLANG_PORT:-8730}:8730"',
            "path: private/anklang.env",
            "required: true",
            'ANKLANG_BIND_HOST: "0.0.0.0"',
            'ANKLANG_REQUIRE_SERVICE_TOKEN: "true"',
            'ANKLANG_MAX_IN_FLIGHT_CHECKS: "${ANKLANG_MAX_IN_FLIGHT_CHECKS:-16}"',
            'ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS: "${ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS:-15}"',
            'ANKLANG_SHUTDOWN_GRACE_SECONDS: "${ANKLANG_SHUTDOWN_GRACE_SECONDS:-30}"',
            'stop_grace_period: "${ANKLANG_STOP_GRACE_PERIOD:-45s}"',
            "/api/v1/live",
            "anklang-problems-data:/app/problems-data",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, compose)
        for unsafe in ("privileged: true", "network_mode: host", "/var/run/docker.sock"):
            self.assertNotIn(unsafe, compose)
        self.assertNotIn("ANKLANG_SERVICE_TOKEN:", compose)

        app_grace_match = re.search(
            r'ANKLANG_SHUTDOWN_GRACE_SECONDS:\s*"\$\{[^:]+:-(\d+)\}"',
            compose,
        )
        stop_grace_match = re.search(
            r'stop_grace_period:\s*"\$\{[^:]+:-(\d+)s\}"', compose
        )
        self.assertIsNotNone(app_grace_match)
        self.assertIsNotNone(stop_grace_match)
        assert app_grace_match is not None and stop_grace_match is not None
        self.assertGreater(
            int(stop_grace_match.group(1)), int(app_grace_match.group(1))
        )

        writable_mounts = re.findall(r"^\s+-\s+[^#\n]+:(/[^:\n]+)\s*$", compose, re.MULTILINE)
        self.assertEqual(writable_mounts, ["/app/problems-data"])

    def test_compose_parser_preserves_runtime_overrides(self) -> None:
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("当前环境没有 Docker CLI。")
        version = subprocess.run(
            [docker, "compose", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if version.returncode != 0:
            self.skipTest("当前 Docker CLI 没有 Compose 子命令。")

        compose_source = (_ROOT / "compose.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            (temporary_root / "compose.yaml").write_text(
                compose_source, encoding="utf-8"
            )
            private_directory = temporary_root / "private"
            private_directory.mkdir(mode=0o700)
            environment_file = private_directory / "anklang.env"
            environment_file.write_text(
                "ANKLANG_SERVICE_TOKEN=synthetic-compose-token\n",
                encoding="utf-8",
            )
            environment_file.chmod(0o600)
            environment = os.environ.copy()
            environment.update(
                {
                    "ANKLANG_MAX_IN_FLIGHT_CHECKS": "7",
                    "ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS": "9",
                    "ANKLANG_SHUTDOWN_GRACE_SECONDS": "11",
                    "ANKLANG_STOP_GRACE_PERIOD": "12s",
                }
            )
            rendered = subprocess.run(
                [
                    docker,
                    "compose",
                    "--env-file",
                    str(environment_file),
                    "-f",
                    str(temporary_root / "compose.yaml"),
                    "config",
                    "--format",
                    "json",
                ],
                cwd=temporary_root,
                env=environment,
                capture_output=True,
                check=False,
                text=True,
            )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        parsed = json.loads(rendered.stdout)
        service = parsed["services"]["anklang"]
        service_environment = service["environment"]
        self.assertEqual(
            service_environment["ANKLANG_SERVICE_TOKEN"],
            "synthetic-compose-token",
        )
        self.assertEqual(service_environment["ANKLANG_MAX_IN_FLIGHT_CHECKS"], "7")
        self.assertEqual(service_environment["ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS"], "9")
        self.assertEqual(service_environment["ANKLANG_SHUTDOWN_GRACE_SECONDS"], "11")
        self.assertIn(service["stop_grace_period"], ("12s", 12_000_000_000))


if __name__ == "__main__":
    unittest.main()
