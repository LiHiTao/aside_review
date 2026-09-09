import sys
import unittest
import plistlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from legal_links import analyze_legal_links


WRAPPER = '''
import SwiftUI
import WebKit
struct LegalPage: UIViewRepresentable {
    let url: URL
    func makeUIView(context: Context) -> WKWebView { WKWebView() }
    func updateUIView(_ webView: WKWebView, context: Context) {
        webView.load(URLRequest(url: url))
    }
}
'''


class LegalLinksTests(unittest.TestCase):
    def scan(self, text, extras=None, incomplete=False):
        files = {Path('/project/App.swift'): text, Path('/project/LegalPage.swift'): WRAPPER}
        files.update({Path('/project') / k: v for k, v in (extras or {}).items()})
        return analyze_legal_links(Path('/project'), files, files, scan_incomplete=incomplete)

    def privacy(self, text, extras=None):
        return [v for v in self.scan(text, extras) if v['kind'] == 'privacy'][0]

    def test_two_protocols_crossfile_wrapper(self):
        result = self.scan('''struct Settings: View { var body: some View {
          VStack {
            NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/privacy")!) }
            NavigationLink("Terms of Service") { LegalPage(url: URL(string:"https://example.com/terms")!) }
          }
        }}''')
        self.assertEqual(['PASS', 'PASS'], [r['status'] for r in result])
        self.assertEqual(['https://example.com/privacy', 'https://example.com/terms'], [r['url'] for r in result])
        self.assertTrue(all(not e['path'].startswith('/') for r in result for e in r['evidence']))

    def test_inline_make_view_load(self):
        wrapper = '''struct LegalPage: UIViewRepresentable {
          let url: URL
          func makeUIView(context: Context) -> WKWebView {
            let web = WKWebView()
            web.load(URLRequest(url: url))
            return web
          }
        }'''
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) }', {'LegalPage.swift': wrapper})
        self.assertEqual('PASS', result['status'])

    def test_constants_and_enum_raw_value(self):
        result = self.scan('''enum LegalURL: String {
          case privacy = "https://example.com/privacy"
          case terms = "https://example.com/terms"
        }
        struct Settings: View { var body: some View {
          NavigationLink("Privacy Policy", destination: LegalPage(url: URL(string: LegalURL.privacy.rawValue)!))
          NavigationLink("Terms of Use") { LegalPage(url: URL(string: LegalURL.terms.rawValue)!) }
        }}''')
        self.assertEqual(['PASS', 'PASS'], [r['status'] for r in result])

    def test_unique_crossfile_static_constant(self):
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string: Config.privacyURL)!) }',
                              {'Config.swift': 'enum Config { static let privacyURL = "https://example.com/p" }'})
        self.assertEqual('PASS', result['status'])

    def test_dynamic_url_stays_unknown(self):
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: config.remoteURL) }')
        self.assertEqual('NOT_VERIFIABLE', result['status'])
        self.assertIsNone(result['url'])

    def test_duplicate_constant_is_not_arbitrarily_selected(self):
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string: Config.privacyURL)!) }', {
            'One.swift': 'enum Config { static let privacyURL = "https://one.com/p" }',
            'Two.swift': 'enum Config { static let privacyURL = "https://two.com/p" }',
        })
        self.assertEqual('NOT_VERIFIABLE', result['status'])

    def test_wrong_loaded_url_fails(self):
        wrapper = WRAPPER.replace('URLRequest(url: url)', 'URLRequest(url: URL(string: "https://wrong.com")!)')
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) }', {'LegalPage.swift': wrapper})
        self.assertEqual('FAIL', result['status'])

    def test_unused_webview_does_not_prove_entry(self):
        result = self.privacy('''Button("Privacy Policy") { showSomething() }
          func ignored() { let web = WKWebView(); web.load(URLRequest(url: URL(string:"https://example.com")!)) }
        ''')
        self.assertEqual('NOT_VERIFIABLE', result['status'])

    def test_mixed_entries_are_both_reported(self):
        result = self.scan('''NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) }
        Button("Privacy Policy") { UIApplication.shared.open(URL(string:"https://example.com/p")!) }''')
        self.assertEqual(['PASS', 'FAIL'], [r['status'] for r in result if r['kind'] == 'privacy'])

    def test_safari_is_only_relevant_when_presented(self):
        result = self.privacy('''Button("Privacy Policy") {
          let unused = SFSafariViewController(url: URL(string:"https://other.com")!)
          let page = LegalController(url: URL(string:"https://example.com/p")!)
          present(page, animated: true)
        }''', {'Controller.swift': '''class LegalController: UIViewController {
          let url: URL
          func viewDidLoad() { let web = WKWebView(); web.load(URLRequest(url: url)); view.addSubview(web) }
        }'''})
        self.assertEqual('PASS', result['status'])
        result = self.privacy('''Button("Privacy Policy") {
          let page = SFSafariViewController(url: URL(string:"https://example.com/p")!)
          present(page, animated: true)
        }''')
        self.assertEqual('FAIL', result['status'])

    def test_local_html_fails(self):
        wrapper = WRAPPER.replace('webView.load(URLRequest(url: url))', 'webView.loadHTMLString("<h1>Policy</h1>", baseURL: nil)')
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) }', {'LegalPage.swift': wrapper})
        self.assertEqual('FAIL', result['status'])

    def test_local_file_fails(self):
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(fileURLWithPath:"policy.html")) }')
        self.assertEqual('FAIL', result['status'])

    def test_link_default_fails(self):
        result = self.privacy('Link("Privacy Policy", destination: URL(string:"https://example.com/p")!)')
        self.assertEqual('FAIL', result['status'])

    def test_button_label_and_action_function(self):
        result = self.privacy('''struct Settings: View {
          func openPrivacy() { UIApplication.shared.open(URL(string:"https://example.com/p")!) }
          var body: some View {
            Button(action: openPrivacy) { Text("Privacy Policy") }
          }
        }''')
        self.assertEqual('FAIL', result['status'])

    def test_sheet_binds_only_triggering_action(self):
        result = self.privacy('''struct Settings: View {
          @State var showing = false
          @State var currentURL: URL?
          var body: some View {
            VStack { Button("Privacy Policy") {
              currentURL = URL(string: "https://example.com/p")!
              showing = true
            }}.sheet(isPresented: $showing) { LegalPage(url: currentURL!) }
          }
        }''')
        self.assertEqual('PASS', result['status'])
        self.assertEqual('https://example.com/p', result['url'])

    def test_scoped_link_override_and_unrelated_sibling(self):
        result = self.scan('''struct Settings: View {
          @State var showing = false
          @State var currentURL: URL?
          var body: some View {
            VStack {
              VStack { Link("Privacy Policy", destination: URL(string:"https://example.com/p")!) }
                .environment(\\.openURL, OpenURLAction { url in
                  currentURL = url
                  showing = true
                  return .handled
                })
                .sheet(isPresented: $showing) { LegalPage(url: currentURL!) }
              Link("Terms of Service", destination: URL(string:"https://example.com/t")!)
            }
          }
        }''')
        self.assertEqual(['PASS', 'FAIL'], [r['status'] for r in result])

    def test_comments_and_prose_are_not_entries(self):
        result = self.scan('''// Button("Privacy Policy") { fake() }
          let prose = "Privacy Policy"
          let example = #"Button(\"Terms of Service\") { WKWebView() }"#
          Text("Privacy Policy")
        ''')
        self.assertEqual(['FAIL', 'FAIL'], [r['status'] for r in result])

    def test_uikit_action_helper(self):
        result = self.privacy('''class Settings: UIViewController {
          func setup() {
            privacyButton.setTitle("Privacy Policy", for: .normal)
            privacyButton.addTarget(self, action: #selector(openPrivacy), for: .touchUpInside)
          }
          @objc func openPrivacy() { showLegal(url: URL(string:"https://example.com/p")!) }
          func showLegal(url: URL) { present(LegalController(url: url), animated: true) }
        }''', {'Controller.swift': '''class LegalController: UIViewController {
          let url: URL
          func viewDidLoad() {
            let web = WKWebView()
            web.load(URLRequest(url: url))
            view.addSubview(web)
          }
        }'''})
        self.assertEqual('PASS', result['status'])

    def test_incomplete_scan_cannot_pass(self):
        result = self.scan('NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) }', incomplete=True)
        self.assertTrue(all(r['status'] == 'NOT_VERIFIABLE' for r in result))

    def test_unused_closure_and_unmounted_webview_do_not_pass(self):
        for body in (
            'let unused = { let w = WKWebView(); w.load(URLRequest(url: URL(string:"https://example.com/p")!)) }',
            'let w = WKWebView(); w.load(URLRequest(url: URL(string:"https://example.com/p")!))',
            'if false { let w = WKWebView(); w.load(URLRequest(url: URL(string:"https://example.com/p")!)) }',
        ):
            with self.subTest(body=body):
                self.assertEqual('NOT_VERIFIABLE', self.privacy('Button("Privacy Policy") { ' + body + ' }')['status'])

    def test_representable_must_return_loaded_instance(self):
        wrapper = '''struct LegalPage: UIViewRepresentable {
          let url: URL
          func makeUIView(context: Context) -> WKWebView {
            let unused = WKWebView()
            unused.load(URLRequest(url: url))
            return WKWebView()
          }
        }'''
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) }', {'LegalPage.swift': wrapper})
        self.assertEqual('NOT_VERIFIABLE', result['status'])

    def test_condition_cannot_hide_an_alternate_route(self):
        wrapper = WRAPPER.replace('webView.load(URLRequest(url: url))', '''
          if useExternal { UIApplication.shared.open(url); return }
          webView.load(URLRequest(url: url))''')
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) }', {'LegalPage.swift': wrapper})
        self.assertEqual('NOT_VERIFIABLE', result['status'])

    def test_unmounted_container_does_not_prove_presentation(self):
        result = self.privacy('Button("Privacy Policy") { present(LegalController(url: URL(string:"https://example.com/p")!), animated:true) }', {
          'Controller.swift': '''class LegalController: UIViewController {
            let url: URL
            func viewDidLoad() {
              let unused = UIView()
              let web = WKWebView()
              web.load(URLRequest(url: url))
              unused.addSubview(web)
            }
          }'''
        })
        self.assertEqual('NOT_VERIFIABLE', result['status'])

    def test_direct_link_override(self):
        result = self.privacy('''struct Settings: View {
          @State var showing = false
          @State var currentURL: URL?
          var body: some View {
            Link("Privacy Policy", destination: URL(string:"https://example.com/p")!)
              .environment(\\.openURL, OpenURLAction { url in
                currentURL = url
                showing = true
                return .handled
              }).sheet(isPresented: $showing) { LegalPage(url: currentURL!) }
          }
        }''')
        self.assertEqual('PASS', result['status'])

    def test_openurl_system_action_is_external(self):
        result = self.privacy('''VStack {
          Link("Privacy Policy", destination: URL(string:"https://example.com/p")!)
        }.environment(\\.openURL, OpenURLAction { url in
          let w = WKWebView()
          w.load(URLRequest(url: url))
          return .systemAction
        })''')
        self.assertEqual('FAIL', result['status'])

    def test_unterminated_comment_marks_scan_incomplete(self):
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) } /* unclosed')
        self.assertEqual('NOT_VERIFIABLE', result['status'])

    def test_objc_title_target_presentation_load(self):
        result = self.scan('', {'Settings.m': '''
          @interface Settings: UIViewController
          @end
          @implementation Settings
          - (void)setup {
            [privacyButton setTitle:@"Privacy Policy" forState:UIControlStateNormal];
            [privacyButton addTarget:self action:@selector(openPrivacy) forControlEvents:UIControlEventTouchUpInside];
          }
          - (void)openPrivacy {
            LegalController *page = [[LegalController alloc] initWithURL:[NSURL URLWithString:@"https://example.com/p"]];
            [self presentViewController:page animated:YES completion:nil];
          }
          @end
        ''', 'Controller.m': '''
          @interface LegalController: UIViewController
          @end
          @implementation LegalController
          - (void)viewDidLoad {
            WKWebView *web = [[WKWebView alloc] init];
            [web loadRequest:[NSURLRequest requestWithURL:self.url]];
            [self.view addSubview:web];
          }
          @end
        '''})
        privacy = [r for r in result if r['kind'] == 'privacy'][0]
        self.assertEqual('PASS', privacy['status'])
        self.assertEqual('https://example.com/p', privacy['url'])

    def test_objc_external_open(self):
        result = self.scan('', {'Settings.m': '''
          @interface Settings: UIViewController
          @end
          @implementation Settings
          - (void)setup {
            [privacyButton setTitle:@"Privacy Policy" forState:UIControlStateNormal];
            [privacyButton addTarget:self action:@selector(openPrivacy) forControlEvents:UIControlEventTouchUpInside];
          }
          - (void)openPrivacy {
            [[UIApplication sharedApplication] openURL:[NSURL URLWithString:@"https://example.com/p"] options:@{} completionHandler:nil];
          }
          @end
        '''})
        privacy = [r for r in result if r['kind'] == 'privacy'][0]
        self.assertEqual('FAIL', privacy['status'])

    def test_missing_protocol_marker(self):
        result = self.scan('let app = 1')
        self.assertTrue(all(r.get('missing') for r in result))

    def test_sequential_page_mutation_uses_presented_url(self):
        result = self.privacy('''Button("Privacy Policy") {
          var page = LegalController(url: URL(string:"https://example.com/p")!)
          present(page, animated: true)
          page = LegalController(url: URL(string:"https://other.com")!)
        }''', {'Controller.swift': '''class LegalController: UIViewController {
          let url: URL
          func viewDidLoad() { let web = WKWebView(); web.load(URLRequest(url: url)); view.addSubview(web) }
        }'''})
        self.assertEqual('PASS', result['status'])
        self.assertEqual('https://example.com/p', result['url'])

    def test_explicit_initializer_binding(self):
        wrapper = WRAPPER.replace('let url: URL', 'let url: URL\n init(documentURL: URL) { self.url = documentURL }')
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(documentURL: URL(string:"https://example.com/p")!) }', {'LegalPage.swift': wrapper})
        self.assertEqual('PASS', result['status'])
        self.assertEqual('https://example.com/p', result['url'])
        wrong = wrapper.replace('self.url = documentURL', 'self.url = URL(string:"https://wrong.com")!')
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(documentURL: URL(string:"https://example.com/p")!) }', {'LegalPage.swift': wrong})
        self.assertEqual('FAIL', result['status'])

    def test_dynamic_input_cannot_be_replaced_with_hardcoded_url(self):
        wrapper = WRAPPER.replace('URLRequest(url: url)', 'URLRequest(url: URL(string:"https://wrong.com")!)')
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: config.remoteURL) }', {'LegalPage.swift': wrapper})
        self.assertEqual('NOT_VERIFIABLE', result['status'])
        self.assertIsNone(result['url'])

    def test_bound_info_plist_key(self):
        result = self.privacy('''NavigationLink("Privacy Policy") {
          LegalPage(url: URL(string: Bundle.main.object(forInfoDictionaryKey: "PrivacyURL") as! String)!)
        }''', {'Info.plist': plistlib.dumps({'PrivacyURL': 'https://example.com/p'}).decode()})
        self.assertEqual('PASS', result['status'])
        self.assertEqual('https://example.com/p', result['url'])

    def test_load_helper_preserves_mounted_webview_binding(self):
        wrapper = WRAPPER.replace('webView.load(URLRequest(url: url))', 'loadLegal(browser: webView, address: url)')
        wrapper += '''\nfunc loadLegal(browser: WKWebView, address: URL) {
          browser.load(URLRequest(url: address))
        }'''
        result = self.privacy('NavigationLink("Privacy Policy") { LegalPage(url: URL(string:"https://example.com/p")!) }', {'LegalPage.swift': wrapper})
        self.assertEqual('PASS', result['status'])

    def test_custom_control_is_unknown_not_missing(self):
        result = self.privacy('SettingsRow(title: "Privacy Policy", action: { openPrivacy() })')
        self.assertEqual('NOT_VERIFIABLE', result['status'])
        self.assertFalse(result.get('missing', False))
        self.assertEqual('NOT_VERIFIABLE', self.privacy('Text("Privacy Policy").onTapGesture { openPrivacy() }')['status'])

    def test_localized_label_maps_to_protocol(self):
        result = self.privacy('NavigationLink("legal.privacy") { LegalPage(url: URL(string:"https://example.com/p")!) }', {
          'en.lproj/Localizable.strings': '"legal.privacy" = "Privacy Policy";'
        })
        self.assertEqual('PASS', result['status'])
        result = self.privacy('Button(privacyTitle) { openPrivacy() }')
        self.assertEqual('NOT_VERIFIABLE', result['status'])

    def test_other_controller_view_assignment_not_mounted(self):
        result = self.privacy('Button("Privacy Policy") { present(LegalController(url: URL(string:"https://example.com/p")!), animated:true) }', {
          'Controller.swift': '''class LegalController: UIViewController {
            let url: URL
            func viewDidLoad() {
              let unusedController = UIViewController()
              let web = WKWebView()
              web.load(URLRequest(url: url))
              unusedController.view = web
            }
          }'''
        })
        self.assertEqual('NOT_VERIFIABLE', result['status'])

    def test_malformed_localization_cannot_be_missing_protocol_failure(self):
        source = '''VStack {
          NavigationLink("legal.p") { LegalPage(url: URL(string:"https://example.com/p")!) }
          NavigationLink("legal.t") { LegalPage(url: URL(string:"https://example.com/t")!) }
        }'''
        fixtures = (
            ('Localizable.strings', '"legal.p" = "Privacy Policy'),
            ('Localizable.strings', '"legal.p" "Privacy Policy";'),
            ('Localizable.strings', '"legal.p" = "Privacy Policy"'),
            ('Localizable.strings', '/* unclosed'),
            ('Localizable.xcstrings', '{"strings":'),
            ('Localizable.xcstrings', '{"strings": []}'),
            ('Localizable.xcstrings', '{"strings": {"legal.p": {"localizations": []}}}'),
            ('Localizable.xcstrings', '{"strings": {"legal.p": {"localizations": {"en": {"stringUnit": {"value": 12}}}}}}'),
        )
        for filename, raw in fixtures:
            with self.subTest(raw=raw):
                result = self.scan(source, {filename: raw})
                self.assertTrue(all(r['status'] == 'NOT_VERIFIABLE' for r in result), result)
                self.assertTrue(all(not r.get('missing') for r in result), result)
        valid = self.scan(source, {'Localizable.strings': '\ufeff/* legal */ "legal.p" = "Privacy Policy";\n "legal.t" = "Terms of Service";'})
        self.assertEqual(['PASS', 'PASS'], [r['status'] for r in valid])
        catalogue = self.scan(source, {'Localizable.xcstrings': '''{"strings": {
          "legal.p": {"localizations": {"en": {"stringUnit": {"value": "Privacy Policy"}}}},
          "legal.t": {"localizations": {"en": {"stringUnit": {"value": "Terms of Service"}}}}
        }}'''})
        self.assertEqual(['PASS', 'PASS'], [r['status'] for r in catalogue])


if __name__ == '__main__':
    unittest.main()
