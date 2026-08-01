from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from anklang import calibrate
from anklang.calibration import (
    CalibrationError,
    CalibrationSettings,
    CorpusArtifactEvidence,
    SampleCancelled,
    SampleSkipped,
    run_calibration,
)
from anklang.vectormath import pack_embedding


def _dataset() -> dict[str, Any]:
    return {
        "schemaVersion": "1",
        "datasetId": "synthetic-private-v1",
        "cases": [
            {
                "caseId": "cal-positive-one",
                "split": "calibration",
                "statement": "合成校准正例甲：对一个短序列执行固定操作。",
                "expectedDuplicateCandidates": [
                    {"source": "synthetic", "externalId": "cal-one"}
                ],
            },
            {
                "caseId": "cal-positive-two",
                "split": "calibration",
                "statement": "合成校准正例乙：在一棵自编小树上回答询问。",
                "expectedDuplicateCandidates": [
                    {"source": "synthetic", "externalId": "cal-two"}
                ],
            },
            {
                "caseId": "cal-negative-one",
                "split": "calibration",
                "statement": "合成校准反例甲：输出两个自编字符的顺序。",
                "expectedDuplicateCandidates": [],
            },
            {
                "caseId": "cal-negative-two",
                "split": "calibration",
                "statement": "合成校准反例乙：判断一个虚构开关是否打开。",
                "expectedDuplicateCandidates": [],
            },
            {
                "caseId": "hold-positive-one",
                "split": "holdout",
                "statement": "合成留出正例甲：计算一组虚构方块的总数。",
                "expectedDuplicateCandidates": [
                    {"source": "synthetic", "externalId": "hold-one"}
                ],
            },
            {
                "caseId": "hold-positive-two",
                "split": "holdout",
                "statement": "合成留出正例乙：寻找自编网络中的一条路线。",
                "expectedDuplicateCandidates": [
                    {"source": "synthetic", "externalId": "hold-two"}
                ],
            },
            {
                "caseId": "hold-negative-one",
                "split": "holdout",
                "statement": "合成留出反例甲：翻转一个虚构灯牌。",
                "expectedDuplicateCandidates": [],
            },
            {
                "caseId": "hold-negative-two",
                "split": "holdout",
                "statement": "合成留出反例乙：原样打印一个自编符号。",
                "expectedDuplicateCandidates": [],
            },
        ],
    }


def _corpus_manifest(
    artifact_hash: str,
    *,
    problem_count: int = 8,
    kind: str = "synthetic-fixture",
    file_name: str = "corpus-artifact.json",
    embedding_rows: int = 0,
    embedding_model: str | None = None,
    embedding_dimensions: int | None = None,
    index_build_revision: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": "1",
        "corpusId": "synthetic-corpus-v1",
        "problemCount": problem_count,
        "sources": [
            {
                "sourceId": "locally-authored-synthetic",
                "revision": "revision-one",
                "license": "project-authored-test-material",
                "licenseReviewed": True,
                "provenance": "generated only for the unit test suite",
                "contentSha256": "a" * 64,
            }
        ],
        "artifact": {
            "kind": kind,
            "fileName": file_name,
            "contentSha256": artifact_hash,
            "problemCount": problem_count,
            "embeddingRows": embedding_rows,
            "embeddingModel": embedding_model,
            "embeddingDimensions": embedding_dimensions,
            "indexBuildRevision": index_build_revision,
        },
    }


def _responses(dataset: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    statements = {case["caseId"]: case["statement"] for case in dataset["cases"]}
    private_title = "候选标题敏感标记"
    private_url = "https://private.invalid/candidate-sensitive-marker"
    raw_model = "模型原始回答敏感标记"

    def candidate(external_id: str, similarity: float) -> dict[str, Any]:
        return {
            "source": "synthetic",
            "externalId": external_id,
            "similarity": similarity,
            "title": private_title,
            "url": private_url,
            "explanation": raw_model,
        }

    return {
        statements["cal-positive-one"]: [candidate("cal-one", 0.95)],
        statements["cal-positive-two"]: [
            candidate("decoy-one", 0.96),
            candidate("cal-two", 0.92),
        ],
        statements["cal-negative-one"]: [candidate("decoy-two", 0.60)],
        statements["cal-negative-two"]: [],
        statements["hold-positive-one"]: [candidate("hold-one", 0.94)],
        statements["hold-positive-two"]: [candidate("hold-two", 0.91)],
        statements["hold-negative-one"]: [candidate("decoy-three", 0.70)],
        statements["hold-negative-two"]: [],
    }


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += 0.01
        return current


class CalibrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="anklang-calibration-test-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "calibration"
        self.workspace.mkdir(mode=0o700)
        self.dataset_path = self.workspace / "dataset.json"
        self.corpus_path = self.workspace / "corpus.json"
        self.artifact_path = self.workspace / "corpus-artifact.json"
        self.dataset = _dataset()
        self._write_private_json(self.dataset_path, self.dataset)
        self._write_private_json(
            self.artifact_path,
            {"schemaVersion": "1", "problemCount": 8},
        )
        self.corpus = _corpus_manifest(
            hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
        )
        self._write_private_json(self.corpus_path, self.corpus)
        self.settings = CalibrationSettings(
            top_ks=(1, 2),
            display_threshold=0.5,
            candidate_thresholds=(0.8, 0.9, 0.95),
            selection_k=2,
            minimum_recall=1.0,
            maximum_false_block_rate=0.0,
        )
        self.backend_descriptor = {
            "backend": "synthetic",
            "revision": "fixed-offline-one",
        }

    @staticmethod
    def _write_private_json(path: Path, value: dict[str, Any]) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def _run(
        self,
        label: str,
        search: Any | None = None,
        *,
        resume: bool = False,
        settings: CalibrationSettings | None = None,
        backend_descriptor: dict[str, Any] | None = None,
        dataset_path: Path | None = None,
        corpus_path: Path | None = None,
    ) -> dict[str, Any]:
        if search is None:
            responses = _responses(self.dataset)
            chosen_search = lambda statement, _maximum: responses[statement]
        else:
            chosen_search = search
        return run_calibration(
            workspace=self.workspace,
            dataset_path=dataset_path or self.dataset_path,
            corpus_manifest_path=corpus_path or self.corpus_path,
            label=label,
            backend_descriptor=backend_descriptor or self.backend_descriptor,
            search=chosen_search,
            settings=settings or self.settings,
            resume=resume,
            clock=_FakeClock(),
        )

    def _replace_synthetic_corpus(self, problem_count: int) -> None:
        self._write_private_json(
            self.artifact_path,
            {"schemaVersion": "1", "problemCount": problem_count},
        )
        self.corpus = _corpus_manifest(
            hashlib.sha256(self.artifact_path.read_bytes()).hexdigest(),
            problem_count=problem_count,
        )
        self._write_private_json(self.corpus_path, self.corpus)

    def test_complete_report_contains_only_safe_aggregate_metrics(self) -> None:
        report = self._run("complete-safe-report")

        self.assertTrue(report["complete"])
        self.assertEqual(report["counts"]["total"], 8)
        self.assertEqual(report["counts"]["statuses"]["success"], 8)
        self.assertEqual(report["metrics"]["calibration"]["recallAtK"], {"1": 0.5, "2": 1.0})
        self.assertEqual(
            report["metrics"]["calibration"]["falsePositiveRateAtDisplayThreshold"],
            0.5,
        )
        self.assertTrue(report["thresholdEvidenceEligible"])
        # 每组只有两个正例和两个反例，只能保留点估计，不能推荐拦截阈值。
        self.assertIsNone(report["thresholdRecommendation"])
        self.assertEqual(report["latencyMs"]["samples"], 8)
        self.assertAlmostEqual(report["latencyMs"]["mean"], 10.0)
        self.assertEqual(
            set(report["bindings"]),
            {"datasetHash", "configHash", "codeHash", "backendHash", "corpusHash"},
        )
        for digest in report["bindings"].values():
            self.assertRegex(digest, r"^[a-f0-9]{64}$")

        run_directory = self.workspace / "runs" / "complete-safe-report"
        checkpoint = run_directory / "checkpoint.json"
        report_path = run_directory / "report.json"
        lock_path = run_directory / "run.lock"
        self.assertEqual(stat.S_IMODE(run_directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(checkpoint.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

        serialized_report = report_path.read_text(encoding="utf-8")
        sensitive_values = [
            *(case["statement"] for case in self.dataset["cases"]),
            "候选标题敏感标记",
            "candidate-sensitive-marker",
            "模型原始回答敏感标记",
            "cal-one",
            "decoy-one",
        ]
        for sensitive in sensitive_values:
            self.assertNotIn(sensitive, serialized_report)

        # 私有检查点只保留复算所需的候选编号和分数，也不保留题面、标题、URL 或模型原话。
        serialized_checkpoint = checkpoint.read_text(encoding="utf-8")
        self.assertIn("cal-one", serialized_checkpoint)
        for sensitive in (
            self.dataset["cases"][0]["statement"],
            "候选标题敏感标记",
            "candidate-sensitive-marker",
            "模型原始回答敏感标记",
        ):
            self.assertNotIn(sensitive, serialized_checkpoint)

    def test_any_error_missing_skip_or_cancel_makes_report_incomplete(self) -> None:
        statements = {case["caseId"]: case["statement"] for case in self.dataset["cases"]}
        responses = _responses(self.dataset)

        def search(statement: str, _maximum: int) -> list[dict[str, Any]] | None:
            if statement == statements["cal-positive-one"]:
                raise RuntimeError("外部错误题面敏感标记")
            if statement == statements["cal-positive-two"]:
                return None
            if statement == statements["cal-negative-one"]:
                raise SampleSkipped("跳过原因敏感标记")
            if statement == statements["cal-negative-two"]:
                raise SampleCancelled("取消原因敏感标记")
            return responses[statement]

        report = self._run("all-incomplete-states", search)

        self.assertFalse(report["complete"])
        self.assertIsNone(report["metrics"])
        self.assertIsNone(report["thresholdRecommendation"])
        self.assertEqual(
            report["counts"]["statuses"],
            {"success": 4, "error": 1, "missing": 1, "skipped": 1, "cancelled": 1},
        )
        serialized = json.dumps(report, ensure_ascii=False)
        for sensitive in (
            "外部错误题面敏感标记",
            "跳过原因敏感标记",
            "取消原因敏感标记",
        ):
            self.assertNotIn(sensitive, serialized)

    def test_malformed_backend_candidate_is_fixed_error_not_raw_output(self) -> None:
        first_statement = self.dataset["cases"][0]["statement"]
        responses = _responses(self.dataset)

        def search(statement: str, _maximum: int) -> list[dict[str, Any]]:
            if statement == first_statement:
                return [
                    {
                        "source": "synthetic",
                        "externalId": "bad",
                        "similarity": True,
                        "title": "损坏候选敏感标记",
                    }
                ]
            return responses[statement]

        report = self._run("malformed-candidate", search)
        self.assertFalse(report["complete"])
        self.assertEqual(report["counts"]["statuses"]["error"], 1)
        self.assertNotIn("损坏候选敏感标记", json.dumps(report, ensure_ascii=False))

    def test_complete_holdout_failure_withholds_threshold_recommendation(self) -> None:
        responses = _responses(self.dataset)
        holdout_statement = self.dataset["cases"][5]["statement"]
        responses[holdout_statement] = [
            {
                "source": "synthetic",
                "externalId": "hold-two",
                "similarity": 0.70,
            }
        ]
        report = self._run(
            "holdout-does-not-pass",
            lambda statement, _maximum: responses[statement],
        )
        self.assertTrue(report["complete"])
        self.assertIsNotNone(report["metrics"])
        self.assertIsNone(report["thresholdRecommendation"])

    def test_threshold_recommendation_requires_large_two_split_evidence(self) -> None:
        cases: list[dict[str, Any]] = []
        responses: dict[str, list[dict[str, Any]]] = {}
        for split in ("calibration", "holdout"):
            for index in range(100):
                statement = f"合成{split}正例-{index}"
                external_id = f"{split}-positive-{index}"
                cases.append(
                    {
                        "caseId": f"{split}-positive-{index}",
                        "split": split,
                        "statement": statement,
                        "expectedDuplicateCandidates": [
                            {"source": "synthetic", "externalId": external_id}
                        ],
                    }
                )
                responses[statement] = [
                    {
                        "source": "synthetic",
                        "externalId": external_id,
                        "similarity": 0.99,
                    }
                ]
            for index in range(100):
                statement = f"合成{split}反例-{index}"
                cases.append(
                    {
                        "caseId": f"{split}-negative-{index}",
                        "split": split,
                        "statement": statement,
                        "expectedDuplicateCandidates": [],
                    }
                )
                responses[statement] = []
        self.dataset = {
            "schemaVersion": "1",
            "datasetId": "large-synthetic-evidence",
            "cases": cases,
        }
        self._write_private_json(self.dataset_path, self.dataset)
        self._replace_synthetic_corpus(400)
        settings = CalibrationSettings(
            top_ks=(1,),
            display_threshold=0.5,
            candidate_thresholds=(0.9,),
            selection_k=1,
            minimum_recall=0.95,
            maximum_false_block_rate=0.05,
        )

        report = self._run(
            "large-evidence",
            lambda statement, _maximum: responses[statement],
            settings=settings,
        )

        recommendation = report["thresholdRecommendation"]
        self.assertTrue(report["complete"])
        self.assertEqual(recommendation["threshold"], 0.9)
        self.assertGreaterEqual(recommendation["holdoutRecallLower95"], 0.95)
        self.assertLessEqual(
            recommendation["holdoutFalseBlockUpper95"], 0.05
        )

    def test_candidate_identity_includes_source(self) -> None:
        holdout_case = self.dataset["cases"][4]
        holdout_case["expectedDuplicateCandidates"] = [
            {"source": "other-source", "externalId": "cal-one"}
        ]
        self._write_private_json(self.dataset_path, self.dataset)
        responses = _responses(self.dataset)
        responses[holdout_case["statement"]] = [
            {
                "source": "synthetic",
                "externalId": "cal-one",
                "similarity": 0.99,
            },
            {
                "source": "other-source",
                "externalId": "cal-one",
                "similarity": 0.94,
            },
        ]

        report = self._run(
            "source-is-part-of-identity",
            lambda statement, _maximum: responses[statement],
        )

        self.assertTrue(report["complete"])
        self.assertEqual(report["metrics"]["holdout"]["recallAtK"]["1"], 0.5)
        self.assertEqual(report["metrics"]["holdout"]["recallAtK"]["2"], 1.0)

    def test_settings_reject_unbounded_k_and_threshold_below_display(self) -> None:
        with self.assertRaises(CalibrationError):
            CalibrationSettings(
                top_ks=(51,),
                selection_k=51,
            ).descriptor()
        with self.assertRaises(CalibrationError):
            CalibrationSettings(
                top_ks=(1,),
                selection_k=1,
                display_threshold=0.8,
                candidate_thresholds=(0.7,),
            ).descriptor()

    def test_actual_corpus_artifact_hash_and_shape_are_verified(self) -> None:
        self._write_private_json(
            self.artifact_path,
            {"schemaVersion": "1", "problemCount": 9},
        )
        with self.assertRaises(CalibrationError):
            self._run("artifact-hash-mismatch")
        self.assertFalse(
            (self.workspace / "runs" / "artifact-hash-mismatch").exists()
        )

        self.corpus = _corpus_manifest(
            hashlib.sha256(self.artifact_path.read_bytes()).hexdigest(),
            problem_count=8,
        )
        self._write_private_json(self.corpus_path, self.corpus)
        with self.assertRaises(CalibrationError):
            self._run("artifact-count-mismatch")
        self.assertFalse(
            (self.workspace / "runs" / "artifact-count-mismatch").exists()
        )

    def test_artifact_change_during_run_preserves_checkpoint_without_report(self) -> None:
        responses = _responses(self.dataset)
        changed = False

        def search(statement: str, _maximum: int) -> list[dict[str, Any]]:
            nonlocal changed
            if not changed:
                changed = True
                self._write_private_json(
                    self.artifact_path,
                    {"schemaVersion": "1", "problemCount": 9},
                )
            return responses[statement]

        with self.assertRaises(CalibrationError):
            self._run("artifact-mutated", search)
        run_directory = self.workspace / "runs" / "artifact-mutated"
        self.assertTrue((run_directory / "checkpoint.json").exists())
        self.assertFalse((run_directory / "report.json").exists())
        # 即使后来把原快照字节放回去，同标签也已经永久失效，不能拿旧候选发布报告。
        self._write_private_json(
            self.artifact_path,
            {"schemaVersion": "1", "problemCount": 8},
        )
        calls: list[str] = []
        with self.assertRaises(CalibrationError):
            self._run(
                "artifact-mutated",
                lambda statement, _maximum: calls.append(statement) or [],
                resume=True,
            )
        self.assertEqual(calls, [])

    def test_dataset_rejects_leakage_duplicates_extra_fields_and_zero_denominators(self) -> None:
        variants: list[tuple[str, Any]] = []

        duplicate_id = _dataset()
        duplicate_id["cases"][4]["caseId"] = duplicate_id["cases"][0]["caseId"]
        variants.append(("duplicate-id", duplicate_id))

        repeated_statement = _dataset()
        repeated_statement["cases"][4]["statement"] = repeated_statement["cases"][0]["statement"]
        variants.append(("cross-split-statement", repeated_statement))

        repeated_expected = _dataset()
        repeated_expected["cases"][4]["expectedDuplicateCandidates"] = [
            {"source": "synthetic", "externalId": "cal-one"}
        ]
        variants.append(("cross-split-expected-id", repeated_expected))

        no_negative = _dataset()
        no_negative["cases"][6]["expectedDuplicateCandidates"] = [
            {"source": "synthetic", "externalId": "hold-three"}
        ]
        no_negative["cases"][7]["expectedDuplicateCandidates"] = [
            {"source": "synthetic", "externalId": "hold-four"}
        ]
        variants.append(("zero-negative-denominator", no_negative))

        extra_title = _dataset()
        extra_title["cases"][0]["title"] = "不允许进入标定数据结构的标题"
        variants.append(("extra-title", extra_title))

        for name, value in variants:
            with self.subTest(name=name):
                self._write_private_json(self.dataset_path, value)
                with self.assertRaises(CalibrationError):
                    self._run(f"invalid-{name}")
                self.assertFalse((self.workspace / "runs" / f"invalid-{name}").exists())

        self.dataset = _dataset()
        self._write_private_json(self.dataset_path, self.dataset)

    def test_private_input_permissions_and_symlinks_are_rejected(self) -> None:
        self.dataset_path.chmod(0o644)
        with self.assertRaises(CalibrationError):
            self._run("public-dataset-mode")
        self.dataset_path.chmod(0o600)

        self.workspace.chmod(0o755)
        with self.assertRaises(CalibrationError):
            self._run("public-workspace-mode")
        self.workspace.chmod(0o700)

        target = self.workspace / "dataset-target.json"
        self._write_private_json(target, self.dataset)
        link = self.workspace / "dataset-link.json"
        link.symlink_to(target.name)
        with self.assertRaises(CalibrationError):
            self._run("symlink-dataset", dataset_path=link)

        real_parent = Path(self.temporary.name) / "real-parent"
        real_parent.mkdir(mode=0o700)
        nested_workspace = real_parent / "nested-calibration"
        nested_workspace.mkdir(mode=0o700)
        nested_dataset = nested_workspace / "dataset.json"
        nested_corpus = nested_workspace / "corpus.json"
        self._write_private_json(nested_dataset, self.dataset)
        self._write_private_json(nested_corpus, self.corpus)
        linked_parent = Path(self.temporary.name) / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(CalibrationError):
            run_calibration(
                workspace=linked_parent / "nested-calibration",
                dataset_path=linked_parent / "nested-calibration" / "dataset.json",
                corpus_manifest_path=linked_parent / "nested-calibration" / "corpus.json",
                label="symlink-ancestor",
                backend_descriptor=self.backend_descriptor,
                search=lambda _statement, _maximum: [],
                settings=self.settings,
            )

        bad_corpus = json.loads(json.dumps(self.corpus))
        bad_corpus["sources"][0]["licenseReviewed"] = False
        self._write_private_json(self.corpus_path, bad_corpus)
        with self.assertRaises(CalibrationError):
            self._run("unreviewed-license")

    def test_unique_label_never_overwrites_report(self) -> None:
        self._run("immutable-label")
        report_path = self.workspace / "runs" / "immutable-label" / "report.json"
        original = report_path.read_bytes()

        with self.assertRaises(CalibrationError):
            self._run("immutable-label")
        with self.assertRaises(CalibrationError):
            self._run("immutable-label", resume=True)
        self.assertEqual(report_path.read_bytes(), original)

    def test_resume_of_unknown_label_does_not_reserve_that_label(self) -> None:
        label = "not-started-yet"
        with self.assertRaises(CalibrationError):
            self._run(label, resume=True)
        self.assertFalse((self.workspace / "runs" / label).exists())
        report = self._run(label)
        self.assertTrue(report["complete"])

    def test_keyboard_interrupt_resume_never_repeats_uncertain_call(self) -> None:
        responses = _responses(self.dataset)
        first_statement = self.dataset["cases"][0]["statement"]
        second_statement = self.dataset["cases"][1]["statement"]
        initial_calls: list[str] = []

        def interrupting_search(statement: str, _maximum: int) -> list[dict[str, Any]]:
            initial_calls.append(statement)
            if statement == second_statement:
                raise KeyboardInterrupt
            return responses[statement]

        with self.assertRaises(KeyboardInterrupt):
            self._run("resume-after-interrupt", interrupting_search)
        run_directory = self.workspace / "runs" / "resume-after-interrupt"
        self.assertTrue((run_directory / "checkpoint.json").exists())
        self.assertFalse((run_directory / "report.json").exists())
        self.assertEqual(initial_calls, [first_statement, second_statement])

        resumed_calls: list[str] = []

        def resumed_search(statement: str, _maximum: int) -> list[dict[str, Any]]:
            resumed_calls.append(statement)
            return responses[statement]

        report = self._run("resume-after-interrupt", resumed_search, resume=True)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["metrics"])
        self.assertEqual(report["counts"]["statuses"]["cancelled"], 1)
        self.assertNotIn(first_statement, resumed_calls)
        self.assertNotIn(second_statement, resumed_calls)
        self.assertEqual(resumed_calls[0], self.dataset["cases"][2]["statement"])

    def test_resume_rejects_changed_binding_and_unsafe_checkpoint(self) -> None:
        responses = _responses(self.dataset)
        second_statement = self.dataset["cases"][1]["statement"]

        def interrupt_on_second(statement: str, _maximum: int) -> list[dict[str, Any]]:
            if statement == second_statement:
                raise KeyboardInterrupt
            return responses[statement]

        with self.assertRaises(KeyboardInterrupt):
            self._run("changed-corpus-binding", interrupt_on_second)
        changed_corpus = json.loads(json.dumps(self.corpus))
        changed_corpus["sources"][0]["revision"] = "revision-two"
        self._write_private_json(self.corpus_path, changed_corpus)
        with self.assertRaises(CalibrationError):
            self._run("changed-corpus-binding", resume=True)

        self._write_private_json(self.corpus_path, self.corpus)
        with self.assertRaises(KeyboardInterrupt):
            self._run("unsafe-checkpoint", interrupt_on_second)
        checkpoint = self.workspace / "runs" / "unsafe-checkpoint" / "checkpoint.json"
        checkpoint.chmod(0o644)
        with self.assertRaises(CalibrationError):
            self._run("unsafe-checkpoint", resume=True)

        with self.assertRaises(KeyboardInterrupt):
            self._run("symlink-checkpoint", interrupt_on_second)
        symlink_checkpoint = (
            self.workspace / "runs" / "symlink-checkpoint" / "checkpoint.json"
        )
        checkpoint_target = symlink_checkpoint.with_name("checkpoint-target.json")
        checkpoint_target.write_bytes(symlink_checkpoint.read_bytes())
        checkpoint_target.chmod(0o600)
        symlink_checkpoint.unlink()
        symlink_checkpoint.symlink_to(checkpoint_target.name)
        with self.assertRaises(CalibrationError):
            self._run("symlink-checkpoint", resume=True)

    def test_resume_lock_prevents_concurrent_duplicate_calls(self) -> None:
        responses = _responses(self.dataset)
        with self.assertRaises(KeyboardInterrupt):
            self._run(
                "concurrent-resume",
                lambda _statement, _maximum: (_ for _ in ()).throw(
                    KeyboardInterrupt
                ),
            )
        lock_path = self.workspace / "runs" / "concurrent-resume" / "run.lock"
        calls: list[str] = []
        with lock_path.open("r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(CalibrationError):
                    self._run(
                        "concurrent-resume",
                        lambda statement, _maximum: (
                            calls.append(statement) or responses[statement]
                        ),
                        resume=True,
                    )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertEqual(calls, [])

    def test_replaced_run_directory_is_not_used_for_output(self) -> None:
        label = "directory-replaced"
        responses = _responses(self.dataset)
        original = self.workspace / "runs" / label
        moved = self.workspace / "runs" / f"{label}-original"
        replaced = False

        def search(statement: str, _maximum: int) -> list[dict[str, Any]]:
            nonlocal replaced
            if not replaced:
                replaced = True
                original.rename(moved)
                original.symlink_to(self.workspace, target_is_directory=True)
            return responses[statement]

        with self.assertRaises(CalibrationError):
            self._run(label, search)
        self.assertTrue((moved / "checkpoint.json").exists())
        self.assertFalse((moved / "report.json").exists())
        self.assertFalse((self.workspace / "report.json").exists())

    def test_backend_descriptor_rejects_secret_fields(self) -> None:
        with self.assertRaises(CalibrationError):
            self._run(
                "secret-descriptor",
                backend_descriptor={"backend": "synthetic", "apiKey": "secret-marker"},
            )
        self.assertFalse((self.workspace / "runs" / "secret-descriptor").exists())


class CalibrateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="anklang-calibration-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "calibration"
        self.workspace.mkdir(mode=0o700)
        self.dataset_path = self.workspace / "dataset.json"
        self.corpus_path = self.workspace / "corpus.json"
        self.artifact_path = self.workspace / "corpus-artifact.json"
        self.dataset = _dataset()
        CalibrationTestCase._write_private_json(self.dataset_path, self.dataset)
        CalibrationTestCase._write_private_json(
            self.artifact_path,
            {"schemaVersion": "1", "problemCount": 8},
        )
        CalibrationTestCase._write_private_json(
            self.corpus_path,
            _corpus_manifest(
                hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
            ),
        )

    def test_cli_uses_injected_backend_without_network_and_prints_only_counts(self) -> None:
        responses = _responses(self.dataset)
        remote_manifest = _corpus_manifest(
            hashlib.sha256(self.artifact_path.read_bytes()).hexdigest(),
            kind="remote-snapshot",
        )
        CalibrationTestCase._write_private_json(self.corpus_path, remote_manifest)

        class FakeBackend:
            def __init__(self) -> None:
                self.store = SimpleNamespace(close=lambda: None)

            def search(self, statement: str, maximum: int) -> SimpleNamespace:
                return SimpleNamespace(
                    candidates=responses[statement][:maximum], degraded=False
                )

        config = SimpleNamespace(
            backend="reverse_proxy",
            search_k=8,
            minimum_similarity=0.5,
            yuantiji_base_url="https://private-endpoint.invalid/v1",
            use_rerank=False,
            yuantiji_timeout_seconds=12.0,
            yuantiji_minimum_interval_seconds=2.0,
            yuantiji_max_retries=1,
            yuantiji_retry_base_delay_seconds=0.5,
            yuantiji_circuit_failure_threshold=3,
            yuantiji_circuit_open_seconds=60.0,
            dashscope_base_url=None,
            dashscope_api_key=None,
        )
        stderr = io.StringIO()
        with (
            patch("anklang.calibrate.load_config", return_value=config),
            patch("anklang.calibrate.build_backend", return_value=FakeBackend()),
            patch("sys.stderr", stderr),
        ):
            code = calibrate.main(
                [
                    "--workspace",
                    str(self.workspace),
                    "--dataset",
                    str(self.dataset_path),
                    "--corpus-manifest",
                    str(self.corpus_path),
                    "--label",
                    "cli-offline-run",
                    "--allow-external-statements",
                ]
            )

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        self.assertIn("complete=true", output)
        for sensitive in (
            self.dataset["cases"][0]["statement"],
            "private-endpoint",
            "候选标题敏感标记",
        ):
            self.assertNotIn(sensitive, output)

    def test_cli_hides_unexpected_exception_text(self) -> None:
        stderr = io.StringIO()
        with (
            patch(
                "anklang.calibrate.load_config",
                side_effect=RuntimeError("unexpected-secret-response-marker"),
            ),
            patch("sys.stderr", stderr),
        ):
            code = calibrate.main(
                [
                    "--workspace",
                    str(self.workspace),
                    "--dataset",
                    str(self.dataset_path),
                    "--corpus-manifest",
                    str(self.corpus_path),
                    "--label",
                    "cli-safe-error",
                ]
            )
        self.assertEqual(code, 2)
        self.assertNotIn("unexpected-secret-response-marker", stderr.getvalue())

    def test_cli_refuses_external_statement_transfer_without_per_run_flag(self) -> None:
        configurations = (
            SimpleNamespace(backend="reverse_proxy", search_k=8),
            SimpleNamespace(
                backend="local_engine",
                search_k=8,
                dashscope_base_url="https://embedding.invalid/v1",
                dashscope_api_key="private-marker",
            ),
        )
        for index, config in enumerate(configurations):
            with self.subTest(backend=config.backend):
                stderr = io.StringIO()
                with (
                    patch("anklang.calibrate.load_config", return_value=config),
                    patch(
                        "anklang.calibrate._build_calibration_backend"
                    ) as build_calibration_backend,
                    patch("sys.stderr", stderr),
                ):
                    code = calibrate.main(
                        [
                            "--workspace",
                            str(self.workspace),
                            "--dataset",
                            str(self.dataset_path),
                            "--corpus-manifest",
                            str(self.corpus_path),
                            "--label",
                            f"external-refused-{index}",
                        ]
                    )
                self.assertEqual(code, 2)
                build_calibration_backend.assert_not_called()
                self.assertIn("--allow-external-statements", stderr.getvalue())
                self.assertFalse(
                    (self.workspace / "runs" / f"external-refused-{index}").exists()
                )
                self.assertNotIn("private-marker", stderr.getvalue())

    def test_cli_requires_threshold_k_and_display_floor_to_match_runtime(self) -> None:
        scenarios = (
            (
                SimpleNamespace(
                    backend="local_engine",
                    search_k=10,
                    minimum_similarity=0.5,
                    dashscope_base_url=None,
                    dashscope_api_key=None,
                ),
                "selection-k",
            ),
            (
                SimpleNamespace(
                    backend="local_engine",
                    search_k=8,
                    minimum_similarity=0.6,
                    dashscope_base_url=None,
                    dashscope_api_key=None,
                ),
                "display-floor",
            ),
        )
        for config, label in scenarios:
            with self.subTest(label=label):
                stderr = io.StringIO()
                with (
                    patch("anklang.calibrate.load_config", return_value=config),
                    patch(
                        "anklang.calibrate._build_calibration_backend"
                    ) as build_calibration_backend,
                    patch("sys.stderr", stderr),
                ):
                    code = calibrate.main(
                        [
                            "--workspace",
                            str(self.workspace),
                            "--dataset",
                            str(self.dataset_path),
                            "--corpus-manifest",
                            str(self.corpus_path),
                            "--label",
                            f"runtime-mismatch-{label}",
                        ]
                    )
                self.assertEqual(code, 2)
                build_calibration_backend.assert_not_called()
                self.assertFalse(
                    (
                        self.workspace
                        / "runs"
                        / f"runtime-mismatch-{label}"
                    ).exists()
                )

    def test_cli_local_calibration_reads_exact_sqlite_snapshot_without_writes(self) -> None:
        database_path = self.workspace / "local-snapshot.db"
        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                """
                CREATE TABLE problems (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    statement TEXT NOT NULL,
                    embedding BLOB,
                    content_hash TEXT NOT NULL,
                    source_updated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            for index, case in enumerate(self.dataset["cases"]):
                expected = case["expectedDuplicateCandidates"]
                external_id = (
                    expected[0]["externalId"] if expected else f"negative-{index}"
                )
                connection.execute(
                    "INSERT INTO problems VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, NULL, ?, ?)",
                    (
                        index + 1,
                        "synthetic",
                        external_id,
                        f"合成标题-{index}",
                        case["statement"],
                        "b" * 64,
                        "2026-01-01T00:00:00.000Z",
                        "2026-01-01T00:00:00.000Z",
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        database_path.chmod(0o600)
        before = hashlib.sha256(database_path.read_bytes()).hexdigest()
        manifest = _corpus_manifest(
            before,
            kind="anklang-sqlite-v1",
            file_name=database_path.name,
        )
        CalibrationTestCase._write_private_json(self.corpus_path, manifest)
        config = SimpleNamespace(
            backend="local_engine",
            search_k=8,
            minimum_similarity=0.5,
            local_db_path=str(database_path),
            local_vector_top_k=20,
            local_keyword_top_k=20,
            dashscope_base_url=None,
            dashscope_api_key=None,
            dashscope_embedding_model="synthetic-embedding",
            dashscope_embedding_dim=16,
        )
        stderr = io.StringIO()
        with (
            patch("anklang.calibrate.load_config", return_value=config),
            patch("sys.stderr", stderr),
        ):
            code = calibrate.main(
                [
                    "--workspace",
                    str(self.workspace),
                    "--dataset",
                    str(self.dataset_path),
                    "--corpus-manifest",
                    str(self.corpus_path),
                    "--label",
                    "local-read-only",
                ]
            )

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(
            hashlib.sha256(database_path.read_bytes()).hexdigest(), before
        )
        self.assertFalse(Path(f"{database_path}-wal").exists())
        self.assertFalse(Path(f"{database_path}-shm").exists())
        report = json.loads(
            (
                self.workspace / "runs" / "local-read-only" / "report.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(report["thresholdEvidenceEligible"])

    def test_local_vector_snapshot_needs_machine_readable_model_provenance(self) -> None:
        database_path = self.workspace / "vector-snapshot.db"
        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                """
                CREATE TABLE problems (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    statement TEXT NOT NULL,
                    embedding BLOB,
                    content_hash TEXT NOT NULL,
                    source_updated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO problems VALUES "
                "(1, 'synthetic', 'one', 'title', NULL, 'statement', ?, ?, "
                "NULL, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')",
                (pack_embedding([0.25, 0.75]), "c" * 64),
            )
            connection.commit()
        finally:
            connection.close()
        database_path.chmod(0o600)
        config = SimpleNamespace(
            backend="local_engine",
            local_db_path=str(database_path),
            dashscope_base_url=None,
            dashscope_api_key=None,
        )
        def evidence() -> CorpusArtifactEvidence:
            return CorpusArtifactEvidence(
                kind="anklang-sqlite-v1",
                path=database_path,
                content_hash=hashlib.sha256(database_path.read_bytes()).hexdigest(),
                problem_count=1,
                embedding_rows=1,
                embedding_model="synthetic-vector-v1",
                embedding_dimensions=2,
                index_build_revision="builder-revision-one",
            )

        verifier = calibrate._corpus_verifier(config)
        self.assertFalse(verifier(evidence()))

        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                "CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO index_metadata VALUES (?, ?)",
                [
                    ("embedding_model", "synthetic-vector-v1"),
                    ("embedding_dimensions", "2"),
                    ("index_build_revision", "builder-revision-one"),
                ],
            )
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(verifier(evidence()))

        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                "UPDATE index_metadata SET value = 'wrong-model' "
                "WHERE key = 'embedding_model'"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertFalse(verifier(evidence()))

    def test_invalid_private_input_does_not_construct_backend(self) -> None:
        self.dataset_path.chmod(0o644)
        config = SimpleNamespace(
            backend="local_engine",
            search_k=8,
            minimum_similarity=0.5,
            local_db_path="unused.db",
            local_vector_top_k=20,
            local_keyword_top_k=20,
            dashscope_base_url=None,
            dashscope_api_key=None,
            dashscope_embedding_model="synthetic-embedding",
            dashscope_embedding_dim=16,
        )
        stderr = io.StringIO()
        with (
            patch("anklang.calibrate.load_config", return_value=config),
            patch(
                "anklang.calibrate._build_calibration_backend"
            ) as build_calibration_backend,
            patch("sys.stderr", stderr),
        ):
            code = calibrate.main(
                [
                    "--workspace",
                    str(self.workspace),
                    "--dataset",
                    str(self.dataset_path),
                    "--corpus-manifest",
                    str(self.corpus_path),
                    "--label",
                    "invalid-input-no-backend",
                ]
            )
        self.assertEqual(code, 2)
        build_calibration_backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
