import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_ios_a_side import Auditor, RULE_ORDER, markdown_report
from scripts.render_audit_pdf import render_audit_pdf, preflight_pdf


class CodeLineIntegrationTests(unittest.TestCase):
    def test_default_scope_counts_project_and_rule_is_in_full_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Business.swift").write_text("let x = 1\n" * 5001)
            report = Auditor(root).run().report()
            self.assertEqual(tuple(f["id"] for f in report["findings"]), RULE_ORDER)
            finding = next(f for f in report["findings"] if f["id"] == "CODE-001")
            self.assertEqual(finding["status"], "PASS")
            self.assertIn("CODE-001", markdown_report(report))

    def test_threshold_report_details_and_no_project_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Aside.swift"
            for policy, lines, expected in (
                ({}, 5000, "FAIL"), ({}, 5001, "PASS"),
                ({"a_side_source_paths": []}, 5000, "FAIL"),
                ({"a_side_source_paths": []}, 5001, "PASS"),
                ({"a_side_source_paths": ["."]}, 5001, "PASS"),
            ):
                with self.subTest(policy=policy, lines=lines):
                    raw = "// documentation\n\n" + "let x = 1 // trailing\n" * lines
                    source.write_text(raw)
                    report = Auditor(root, policy).run().report()
                    finding = next(f for f in report["findings"] if f["id"] == "CODE-001")
                    self.assertEqual(finding["status"], expected, finding)
                    self.assertTrue(finding["details"])
                    self.assertIn("Aside.swift", json.dumps(finding))
                    self.assertIn("Aside.swift", markdown_report(report))
                    self.assertEqual(source.read_text(), raw)
                    self.assertEqual(list(root.iterdir()), [source])
                    self.assertNotIn(str(root), json.dumps(finding))

    def test_pdf_contains_code_line_rule_and_all_existing_rules(self):
        try:
            preflight_pdf()
        except RuntimeError as error:
            self.skipTest(str(error))
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "Aside.swift").write_text("let x = 1\n" * 5001)
            report = Auditor(root, {"a_side_source_paths": ["."]}).run().report()
            output = Path(directory) / "report.pdf"
            render_audit_pdf(report, output)
            reader = PdfReader(output)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            for rule in RULE_ORDER:
                self.assertIn(rule, text)
            self.assertIn("5,001", text.replace("5001", "5,001"))
            self.assertNotIn("Aside.swift", text)
            for page in reader.pages:
                self.assertAlmostEqual(float(page.mediabox.width), 595.28, delta=1)
                self.assertAlmostEqual(float(page.mediabox.height), 841.89, delta=1)
