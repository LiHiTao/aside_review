#!/usr/bin/env python3
import builtins
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts.render_audit_pdf import preflight_pdf, render_audit_pdf  # noqa: E402
from scripts.audit_ios_a_side import main  # noqa: E402


def sample_report() -> dict:
    statuses = ("PASS", "FAIL", "NOT_VERIFIABLE", "WARN")
    findings = []
    for index, status in enumerate(statuses, start=1):
        findings.append({
            "id": f"SAMPLE-{index:03d}",
            "status": status,
            "severity": "high" if status == "FAIL" else "medium" if status != "PASS" else "info",
            "title": "PDF 中文排版检查",
            "expected": "长文本、路径和状态标签可以正确换行",
            "actual": "这是一段用于检查 PDF 表格换行和中文字体嵌入的示例结论。" * 3,
            "evidence": [{
                "path": "VeryLongProjectPath/Feature/Configuration/AppStoreMetadata.json",
                "line": index,
                "excerpt": "证据摘录保持清晰，不显示绝对路径。" * 3,
            }],
            "manual_check": "在真机、沙盒或 App Store Connect 中完成相应复核。" if status != "PASS" else None,
        })
    return {
        "schema_version": "2.0",
        "project_root": ".",
        "only_failures": False,
        "summary": {status: 1 for status in statuses},
        "findings": findings,
    }


class PdfTests(unittest.TestCase):
    def setUp(self) -> None:
        # PDF tests exercise rendering only; updater behavior is covered separately.
        updater = patch("scripts.audit_ios_a_side.ensure_latest", return_value=False)
        updater.start()
        self.addCleanup(updater.stop)

    def test_preflight_reports_missing_reportlab(self) -> None:
        real_import = builtins.__import__

        def rejecting_import(name, *args, **kwargs):
            if name == "reportlab" or name.startswith("reportlab."):
                raise ImportError("simulated missing reportlab")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=rejecting_import):
            with self.assertRaisesRegex(RuntimeError, "缺少 ReportLab"):
                preflight_pdf()

    def test_render_pdf_when_dependencies_are_available(self) -> None:
        try:
            preflight_pdf()
        except RuntimeError as error:
            self.skipTest(str(error))
        from pypdf import PdfReader

        with tempfile.TemporaryDirectory(prefix="ios-aside-review-pdf-test-") as directory:
            output = Path(directory) / "ios-aside-review.pdf"
            render_audit_pdf(sample_report(), output)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 1000)
            self.assertEqual(output.read_bytes()[:4], b"%PDF")
            self.assertFalse(output.with_name(output.name + ".tmp").exists())
            text = "\n".join(page.extract_text() or "" for page in PdfReader(output).pages)
            self.assertIn("完整检查清单", text)
            for finding in sample_report()["findings"]:
                self.assertIn(finding["id"], text)
            for omitted in (
                "审计结论概览",
                "详细检查结果",
                "项目标识",
                "规则版本",
                "严重级别",
                "人工复核",
                "位置与摘录",
                "VeryLongProjectPath",
                "证据摘录保持清晰",
            ):
                self.assertNotIn(omitted, text)

    def test_cli_dependency_failure_leaves_no_output_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-cli-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            output = Path(directory) / "report"
            with patch("scripts.render_audit_pdf.preflight_pdf", side_effect=RuntimeError("simulated missing dependency")):
                with redirect_stderr(io.StringIO()):
                    result = main([str(project), "--format", "pdf", "--output-dir", str(output)])
            self.assertEqual(result, 2)
            self.assertFalse(output.exists())

    def test_cli_default_writes_only_pdf(self) -> None:
        try:
            preflight_pdf()
        except RuntimeError as error:
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-cli-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            output = Path(directory) / "report"
            with redirect_stdout(io.StringIO()):
                result = main([str(project), "--output-dir", str(output)])
            self.assertEqual(result, 1)
            self.assertEqual({path.name for path in output.iterdir()}, {"ios-aside-review.pdf"})

    def test_cli_all_writes_three_reports(self) -> None:
        try:
            preflight_pdf()
        except RuntimeError as error:
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-cli-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            output = Path(directory) / "report"
            with redirect_stdout(io.StringIO()):
                result = main([str(project), "--format", "all", "--output-dir", str(output)])
            self.assertEqual(result, 1)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"ios-aside-review.json", "ios-aside-review.md", "ios-aside-review.pdf"},
            )


if __name__ == "__main__":
    unittest.main()
