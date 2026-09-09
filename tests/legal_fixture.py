from pathlib import Path

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


def successful_probe(url: str) -> dict:
    return {"status": "PASS", "actual": "HTTP 200，返回非空可读页面", "manual_check": None,
            "original_url": url, "final_url": url, "http_status": 200,
            "content_type": "text/html", "checked_at": "2026-09-09T00:00:00Z"}
