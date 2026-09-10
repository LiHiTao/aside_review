from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from scripts.audit_ios_a_side import Auditor, RULE_ORDER, markdown_report
try:
    from .legal_fixture import LEGAL_SOURCE, write_legal_fixture, assert_no_audit_network
except ImportError:
    from legal_fixture import LEGAL_SOURCE, write_legal_fixture, assert_no_audit_network


class LegalIntegrationTests(unittest.TestCase):
    def setUp(self):
        assert_no_audit_network(self)

    def test_navigation_wrapped_protocols_are_checked_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Gate.swift").write_text('''import UIKit
class Gate: UIViewController {
    func setup() {
        privacyButton.setTitle("Privacy Policy", for: .normal)
        privacyButton.addTarget(self, action: #selector(showPrivacy), for: .touchUpInside)
        termsButton.setTitle("Terms & Support", for: .normal)
        termsButton.addTarget(self, action: #selector(showTerms), for: .touchUpInside)
    }
    @objc private func showPrivacy() {
        let pane = LegalPane(url: Legal.privacyURL, heading: "Privacy Policy")
        let nav = UINavigationController(rootViewController: pane)
        nav.modalPresentationStyle = .fullScreen
        present(nav, animated: true)
    }
    @objc private func showTerms() {
        let pane = LegalPane(url: Legal.termsURL, heading: "Terms & Support")
        present(UINavigationController(rootViewController: pane), animated: true)
    }
}
''')
            (root / "Legal.swift").write_text('''import UIKit
import WebKit
enum Legal {
    static let privacyURL = URL(string: "https://example.com/privacy")!
    static let termsURL = URL(string: "https://example.com/terms")!
}
class LegalPane: UIViewController {
    let url: URL
    init(url: URL, heading: String) {
        self.url = url
        super.init(nibName: nil, bundle: nil)
        title = heading
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) unavailable") }
    override func viewDidLoad() {
        super.viewDidLoad()
        let web = WKWebView()
        view.addSubview(web)
        web.load(URLRequest(url: url))
    }
}
''')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            findings = {f["id"]: f for f in Auditor(root).run().report()["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "PASS", findings["LEGAL-001"])
            self.assertNotIn("LEGAL-002", findings)
            self.assertEqual({d["url"] for d in findings["LEGAL-001"]["details"]}, {"https://example.com/privacy", "https://example.com/terms"})
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_routes_schema_and_read_only_without_accessibility_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_legal_fixture(root)
            original = (root / "LegalViews.swift").read_bytes()
            report = Auditor(root).run().report()
            findings = {f["id"]: f for f in report["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "PASS", findings["LEGAL-001"])
            self.assertNotIn("LEGAL-002", findings)
            self.assertEqual(report["schema_version"], "2.0")
            self.assertEqual(tuple(findings), RULE_ORDER)
            self.assertEqual(len(RULE_ORDER), 17)
            self.assertEqual(sum(report["summary"].values()), 17)
            markdown = markdown_report(report)
            for removed in ("LEGAL-002", "协议 URL 可访问性", "http_status", "checked_at", "联网记录"):
                self.assertNotIn(removed, markdown)
            self.assertTrue(all("network" not in d for d in findings["LEGAL-001"]["details"]))
            self.assertEqual((root / "LegalViews.swift").read_bytes(), original)
            self.assertEqual(len(list(root.iterdir())), 1)

    def test_same_url_keeps_all_static_protocol_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Legal.swift").write_text(LEGAL_SOURCE.replace("https://example.com/privacy", "https://example.com/legal#privacy").replace("https://example.com/terms", "https://example.com/legal#terms"))
            report = Auditor(root).run().report()
            finding = next(f for f in report["findings"] if f["id"] == "LEGAL-001")
            self.assertEqual(finding["status"], "PASS", finding)
            self.assertEqual(len(finding["details"]), 2)
            self.assertEqual({d["url"] for d in finding["details"]}, {"https://example.com/legal#privacy", "https://example.com/legal#terms"})

    def test_undeployed_urls_do_not_change_static_wkwebview_verdict(self):
        for hostname in ("not-deployed.invalid", "offline.example.invalid", "127.0.0.1:1"):
            with self.subTest(hostname=hostname), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "Legal.swift").write_text(LEGAL_SOURCE.replace("example.com", hostname))
                findings = {f["id"]: f for f in Auditor(root).run().report()["findings"]}
                self.assertEqual(findings["LEGAL-001"]["status"], "PASS")
                self.assertNotIn("LEGAL-002", findings)

    def test_missing_protocols_make_no_network_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "App.swift").write_text("import SwiftUI\nstruct AppView: View { var body: some View { Text(\"Welcome\") } }")
            findings = {f["id"]: f for f in Auditor(root).run().report()["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "FAIL")
            self.assertNotIn("LEGAL-002", findings)

    def test_any_bad_route_keeps_aggregate_failure(self):
        entries = [
            {"kind": "privacy", "status": "PASS", "actual": "WKWebView", "url": "https://example.com/privacy", "evidence": []},
            {"kind": "privacy", "status": "FAIL", "actual": "外部浏览器", "url": "https://example.com/privacy", "evidence": []},
            {"kind": "terms", "status": "PASS", "actual": "WKWebView", "url": "https://example.com/terms", "evidence": []},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch("scripts.audit_ios_a_side.analyze_legal_links", return_value=entries):
                findings = {f["id"]: f for f in Auditor(Path(directory)).run().report()["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "FAIL")
            self.assertEqual(len(findings["LEGAL-001"]["details"]), 3)
            self.assertNotIn("LEGAL-002", findings)

    def test_source_decoding_errors_are_reported_to_legal_analyzer(self):
        for suffix in (".swift", ".strings", ".xcstrings"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ("Bad" + suffix)).write_bytes(b"\xff\xfeinvalid utf8")
                entry = {"kind": "privacy", "status": "NOT_VERIFIABLE", "actual": "源码不完整", "url": None, "evidence": []}
                with patch("scripts.audit_ios_a_side.analyze_legal_links", return_value=[entry]) as analyzer:
                    Auditor(root).run()
                self.assertTrue(analyzer.call_args.kwargs["scan_incomplete"])

    def test_binary_plist_does_not_mark_legal_source_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_legal_fixture(root)
            (root / "Info.plist").write_bytes(plistlib.dumps({"CFBundleName": "Legal"}, fmt=plistlib.FMT_BINARY))
            finding = next(f for f in Auditor(root).run().report()["findings"] if f["id"] == "LEGAL-001")
            self.assertEqual(finding["status"], "PASS", finding)

    def test_pdf_contains_static_legal_row_and_all_17_rules(self):
        from scripts.render_audit_pdf import preflight_pdf, render_audit_pdf
        try:
            preflight_pdf()
        except RuntimeError as error:
            self.skipTest(str(error))
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            write_legal_fixture(root)
            report = Auditor(root).run().report()
            target = Path(directory) / "report.pdf"
            render_audit_pdf(report, target)
            reader = PdfReader(target)
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
            for rule in RULE_ORDER:
                self.assertIn(rule, text)
            self.assertEqual(len(RULE_ORDER), 17)
            self.assertNotIn("LEGAL-002", text)
            self.assertNotIn("协议 URL 可访问性", text)
            for page in reader.pages:
                self.assertAlmostEqual(float(page.mediabox.width), 595.28, delta=1)
                self.assertAlmostEqual(float(page.mediabox.height), 841.89, delta=1)
