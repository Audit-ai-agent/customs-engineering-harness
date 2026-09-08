from __future__ import annotations

import unittest

from harness import validation_report


class ValidationReportTests(unittest.TestCase):
    def test_summaries_read_the_tool_output_not_the_exit_code(self) -> None:
        self.assertEqual(
            validation_report.summarize_output(["python", "-m", "pytest", "-q"], "336 passed, 4 skipped in 38s", ""),
            "336 passed, 4 skipped",
        )
        self.assertEqual(
            validation_report.summarize_output(["corepack", "npm", "run", "check:quick"], "Tests  2 failed | 30 passed (32)", ""),
            "30 passed, 2 failed",
        )
        self.assertEqual(validation_report.summarize_output(["python", "-m", "mypy"], "Found 3 errors in 2 files", ""), "3 errors")
        self.assertEqual(validation_report.summarize_output(["python", "-m", "ruff", "check", "."], "All checks passed!", ""), "clean")
        self.assertEqual(
            validation_report.summarize_output(["python", "tools/verify_contract_snapshots.py"], "", "paths are not clean:\n M docs/openapi-v1.json"),
            "snapshot paths not clean (uncommitted)",
        )

    def test_markdown_reports_failures_as_rows_and_keeps_the_approval_block(self) -> None:
        report = {
            "schema": validation_report.REPORT_SCHEMA,
            "generated_at": "2026-09-08T00:00:00+00:00",
            "workspace_root": "C:/ws",
            "repositories": [
                {"repository": "backend", "directory": "customs-backend", "branch": "main", "dirty": True, "origin": "o/r", "revision": "a" * 40},
            ],
            "checks": [
                {"layer": "L2", "repository": "backend", "command": ["python", "-m", "pytest"], "status": "PASS", "summary": "336 passed", "duration_seconds": 38.7},
                {"layer": "L1", "repository": "backend", "command": ["python", "-m", "mypy"], "status": "FAIL", "summary": "3 errors", "duration_seconds": 9.1},
            ],
            "cross_contracts": [
                {"consumer": "frontend", "provider": "backend", "consumer_path": "contracts/backend-openapi-v1.json", "provider_path": "docs/openapi-v1.json", "status": "PASS", "detail": "abc123"},
            ],
            "evidence": {
                "scenario_transcripts": {"directory": "C:/ws/_local/state/scenario-runs", "complete_runs": 60, "latest": "2026-08-22 10:00 UTC"},
                "functional_uat_manifests": {"directory": "C:/ws/_artifacts/functional-uat-law-history", "count": 1, "latest": "2026-08-13 14:09 UTC"},
                "document_preview_pointers": {"pdf-preview-active.json": "activated"},
                "ocr_gold_evaluation_doc": "C:/ws/customs-ai-agent/docs/R1_SUPERVISED_NATIVE_PDF_EVALUATION.md",
            },
        }
        text = validation_report.render_markdown(report)
        self.assertIn("| L1 | backend | `python -m mypy` | ❌ FAIL | 3 errors | 9 |", text)
        self.assertIn("합계: PASS 1 · FAIL 1 · SKIP 0", text)
        self.assertIn("| 있음 |", text)  # dirty working tree is stated, not hidden
        self.assertIn("transcript(요약 파일 포함) 보유: **60건**", text)
        self.assertIn("| 품질 승인자 |  |  |  |", text)
        self.assertNotIn("추정", text.split("## 1.")[1].split("## 2.")[0])


if __name__ == "__main__":
    unittest.main()
