from pathlib import Path
from unittest.mock import patch

LEGAL_SOURCE = '''import SwiftUI
import WebKit
struct LegalPage: UIViewRepresentable {
    let url: URL
    func makeUIView(context: Context) -> WKWebView { WKWebView() }
    func updateUIView(_ webView: WKWebView, context: Context) {
        webView.load(URLRequest(url: url))
    }
}
struct LegalSettings: View {
    var body: some View {
        VStack {
            NavigationLink("Privacy Policy") {
                LegalPage(url: URL(string: "https://example.com/privacy")!)
            }
            NavigationLink("Terms of Service") {
                LegalPage(url: URL(string: "https://example.com/terms")!)
            }
        }
    }
}
'''


def write_legal_fixture(root: Path) -> None:
    (root / "LegalViews.swift").write_text(LEGAL_SOURCE)


def assert_no_audit_network(test_case) -> None:
    """Fail even if audit code catches a forbidden network attempt internally."""
    for target in (
        "socket.getaddrinfo", "socket.socket", "socket.create_connection",
        "urllib.request.urlopen", "urllib.request.OpenerDirector.open",
        "http.client.HTTPConnection.connect", "http.client.HTTPSConnection.connect",
    ):
        guard = patch(target, side_effect=AssertionError("Static audits must not request protocol URLs"))
        mocked = guard.start()
        test_case.addCleanup(guard.stop)
        test_case.addCleanup(mocked.assert_not_called)
