from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts/prepare-local-query-acceptance.py"
_SPEC = importlib.util.spec_from_file_location("prepare_local_query_acceptance", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_PREPARE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PREPARE)


class SourceBindingTests(unittest.TestCase):
    def _fixture(self) -> tuple[Path, list[Path], dict[str, object], dict[str, object], str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        files: list[Path] = []
        entries: list[dict[str, object]] = []
        for source_id, raw in (
            ("source-000001", b"synthetic-alpha"),
            ("source-000002", b"synthetic-beta"),
        ):
            path = root / f"{source_id}.md"
            path.write_bytes(raw)
            files.append(path)
            entries.append(
                {
                    "sourceId": source_id,
                    "sourceSha256": hashlib.sha256(raw).hexdigest(),
                    "byteLength": len(raw),
                }
            )
        report = {"sources": entries}
        expected = hashlib.sha256(
            json.dumps(
                {"version": 1, "sources": entries},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        marker = {
            "sourceCount": 2,
            "unresolvedItemCount": 0,
            "sourceSetSha256": expected,
        }
        return root, files, report, marker, expected

    def test_matches_authoritative_report_marker_algorithm(self) -> None:
        _root, files, report, marker, expected = self._fixture()
        original_count = _PREPARE.EXPECTED_COUNT
        _PREPARE.EXPECTED_COUNT = 2
        try:
            actual = _PREPARE._authoritative_source_set_sha256(
                report, marker, files
            )
        finally:
            _PREPARE.EXPECTED_COUNT = original_count
        self.assertEqual(actual, expected)

    def test_rejects_marker_identity_mismatch(self) -> None:
        _root, files, report, marker, _expected = self._fixture()
        marker["sourceSetSha256"] = "0" * 64
        original_count = _PREPARE.EXPECTED_COUNT
        _PREPARE.EXPECTED_COUNT = 2
        try:
            with self.assertRaises(_PREPARE.AcceptanceError) as context:
                _PREPARE._authoritative_source_set_sha256(
                    report, marker, files
                )
        finally:
            _PREPARE.EXPECTED_COUNT = original_count
        self.assertEqual(context.exception.code, "ACCEPTANCE_SOURCE_SET_MARKER_MISMATCH")


if __name__ == "__main__":
    unittest.main()
