from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.audit_ios_a_side import Auditor, RULE_ORDER, markdown_report
try:
    from .legal_fixture import LEGAL_SOURCE, write_legal_fixture, successful_probe
except ImportError:
    from legal_fixture import LEGAL_SOURCE, write_legal_fixture, successful_probe


class LegalIntegrationTests(unittest.TestCase):
    def test_navigation_wrapped_protocols_reach_network_probe(self):
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
            probe = Mock(side_effect=successful_probe)
            findings = {f["id"]: f for f in Auditor(root, legal_url_probe=probe).run().report()["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "PASS", findings["LEGAL-001"])
            self.assertEqual(findings["LEGAL-002"]["status"], "PASS", findings["LEGAL-002"])
            self.assertEqual({call.args[0] for call in probe.call_args_list}, {"https://example.com/privacy", "https://example.com/terms"})
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_routes_network_metadata_schema_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_legal_fixture(root)
            original = (root / "LegalViews.swift").read_bytes()
            probe = Mock(side_effect=successful_probe)
            with patch("socket.getaddrinfo", side_effect=AssertionError("No real DNS in tests")):
                report = Auditor(root, legal_url_probe=probe).run().report()
            findings = {f["id"]: f for f in report["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "PASS", findings["LEGAL-001"])
            self.assertEqual(findings["LEGAL-002"]["status"], "PASS", findings["LEGAL-002"])
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(report["schema_version"], "2.0")
            self.assertEqual(tuple(findings), RULE_ORDER)
            self.assertIn("http_status=200", markdown_report(report))
            self.assertTrue(all(d["network"]["checked_at"] for d in findings["LEGAL-002"]["details"]))
            self.assertEqual((root / "LegalViews.swift").read_bytes(), original)
            self.assertEqual(len(list(root.iterdir())), 1)

    def test_same_url_is_checked_once_but_all_protocol_entries_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Legal.swift").write_text(LEGAL_SOURCE.replace("https://example.com/privacy", "https://example.com/legal#privacy").replace("https://example.com/terms", "https://example.com/legal#terms"))
            probe = Mock(side_effect=successful_probe)
            report = Auditor(root, legal_url_probe=probe).run().report()
            finding = next(f for f in report["findings"] if f["id"] == "LEGAL-002")
            self.assertEqual(finding["status"], "PASS", finding)
            probe.assert_called_once_with("https://example.com/legal")
            self.assertEqual(len(finding["details"]), 2)

    def test_unavailable_url_does_not_change_wkwebview_route_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_legal_fixture(root)
            def probe(url):
                return {**successful_probe(url), "status": "NOT_VERIFIABLE", "actual": "连接超时", "http_status": None}
            findings = {f["id"]: f for f in Auditor(root, legal_url_probe=probe).run().report()["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "PASS")
            self.assertEqual(findings["LEGAL-002"]["status"], "NOT_VERIFIABLE")

    def test_missing_protocols_make_no_network_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "App.swift").write_text("import SwiftUI\nstruct AppView: View { var body: some View { Text(\"Welcome\") } }")
            probe = Mock(side_effect=AssertionError("Unexpected URL request"))
            findings = {f["id"]: f for f in Auditor(root, legal_url_probe=probe).run().report()["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "FAIL")
            self.assertEqual(findings["LEGAL-002"]["status"], "FAIL")
            probe.assert_not_called()

    def test_any_bad_route_keeps_aggregate_failure(self):
        entries = [
            {"kind": "privacy", "status": "PASS", "actual": "WKWebView", "url": "https://example.com/privacy", "evidence": []},
            {"kind": "privacy", "status": "FAIL", "actual": "外部浏览器", "url": "https://example.com/privacy", "evidence": []},
            {"kind": "terms", "status": "PASS", "actual": "WKWebView", "url": "https://example.com/terms", "evidence": []},
        ]
        with tempfile.TemporaryDirectory() as directory:
            probe = Mock(side_effect=successful_probe)
            with patch("scripts.audit_ios_a_side.analyze_legal_links", return_value=entries):
                findings = {f["id"]: f for f in Auditor(Path(directory), legal_url_probe=probe).run().report()["findings"]}
            self.assertEqual(findings["LEGAL-001"]["status"], "FAIL")
            self.assertEqual(len(findings["LEGAL-001"]["details"]), 3)
            self.assertEqual(findings["LEGAL-002"]["status"], "PASS")
            self.assertEqual(probe.call_count, 2)

    def test_source_decoding_errors_are_reported_to_legal_analyzer(self):
        for suffix in (".swift", ".strings", ".xcstrings"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ("Bad" + suffix)).write_bytes(b"\xff\xfeinvalid utf8")
                entry = {"kind": "privacy", "status": "NOT_VERIFIABLE", "actual": "源码不完整", "url": None, "evidence": []}
                probe = Mock(side_effect=AssertionError("Unexpected request"))
                with patch("scripts.audit_ios_a_side.analyze_legal_links", return_value=[entry]) as analyzer:
                    Auditor(root, legal_url_probe=probe).run()
                self.assertTrue(analyzer.call_args.kwargs["scan_incomplete"])
                probe.assert_not_called()

    def test_binary_plist_does_not_mark_legal_source_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_legal_fixture(root)
            (root / "Info.plist").write_bytes(plistlib.dumps({"CFBundleName": "Legal"}, fmt=plistlib.FMT_BINARY))
            finding = next(f for f in Auditor(root, legal_url_probe=successful_probe).run().report()["findings"] if f["id"] == "LEGAL-001")
            self.assertEqual(finding["status"], "PASS", finding)

    def test_pdf_contains_both_legal_rows(self):
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
            report = Auditor(root, legal_url_probe=successful_probe).run().report()
            target = Path(directory) / "report.pdf"
            render_audit_pdf(report, target)
            reader = PdfReader(target)
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
            for rule in RULE_ORDER:
                self.assertIn(rule, text)
            for page in reader.pages:
                self.assertAlmostEqual(float(page.mediabox.width), 595.28, delta=1)
                self.assertAlmostEqual(float(page.mediabox.height), 841.89, delta=1)
