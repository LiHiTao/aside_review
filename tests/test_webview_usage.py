"""Project-level simplified WKWebView evidence; deliberately no entry matching."""
from pathlib import Path
import unittest
from scripts.legal_links import analyze_webview_usage


class WebViewUsageTests(unittest.TestCase):
    def scan(self, source, suffix='.swift', extra=None, incomplete=False):
        files = {Path('/project/Main' + suffix): source}
        files.update({Path('/project') / name: text for name, text in (extra or {}).items()})
        return analyze_webview_usage(Path('/project'), files, scan_incomplete=incomplete)

    def test_no_entry_and_uncalled_methods_still_count(self):
        source = '''class Pane {
          let web = WKWebView()
          func unused() { if ready { web.load(request) } }
        }'''
        self.assertEqual('PASS', self.scan(source)['status'])

    def test_swift_explicit_type_parameters_aliases_and_load_forms(self):
        for load in ('load(request)', 'loadHTMLString(content, baseURL: nil)', 'loadFileURL(file, allowingReadAccessTo: directory)'):
            for source in (
                'func run(web: WKWebView) { web.' + load + ' }',
                'func run(web: WebKit.WKWebView) { let alias = web\n alias.' + load + ' }',
                'class Pane { var web: WKWebView!\n func run() { self.web?.' + load + ' } }',
                'func run() { let web = WebKit.WKWebView()\n web.' + load + ' }',
            ):
                with self.subTest(source=source):
                    self.assertEqual('PASS', self.scan(source)['status'])

    def test_objc_init_property_parameter_and_alias(self):
        for source in (
            '@implementation Pane\n -(void)run { WKWebView *web = [[WKWebView alloc] init]; [web loadRequest:request]; }\n@end',
            '@implementation Pane\n -(void)run:(WKWebView *)web { [web loadFileURL:file allowingReadAccessToURL:directory]; }\n@end',
            '@interface Pane : NSObject\n@property WKWebView *web;\n@end\n@implementation Pane\n -(void)run { [self.web loadHTMLString:html baseURL:nil]; }\n@end',
            '@implementation Pane\n -(void)run { WKWebView *web = [[WKWebView alloc] init]; id alias = web; [alias loadRequest:request]; }\n@end',
        ):
            with self.subTest(source=source):
                self.assertEqual('PASS', self.scan(source, '.m')['status'])

    def test_objc_separate_header(self):
        result = self.scan('@implementation Pane\n -(void)run { [_web loadRequest:request]; }\n@end', '.m',
                           {'Pane.h': '@interface Pane : NSObject\n@property WKWebView *web;\n@end'})
        self.assertEqual('PASS', result['status'])

    def test_swift_local_shadow_and_sibling_methods_do_not_lend_type(self):
        for source in (
            'class Pane { let web = WKWebView()\n func run() { let web = Other()\n web.load(request) } }',
            'func one() { let web = WKWebView() }\nfunc two() { web.load(request) }',
            'class A { let web = WKWebView() }\nclass B { func run() { web.load(request) } }',
            'class A { let web = WKWebView()\nfunc run(web: Other) { web.load(request) } }',
        ):
            with self.subTest(source=source):
                self.assertEqual('NOT_VERIFIABLE', self.scan(source)['status'])

    def test_comments_and_string_examples_do_not_count(self):
        for source in ('// let web = WKWebView(); web.load(request)',
                       '/* WKWebView().load(request) */',
                       'let example = "WKWebView().load(request)"'):
            self.assertEqual('FAIL', self.scan(source)['status'])

    def test_other_types_load_and_delegate_only_are_not_passed(self):
        self.assertEqual('FAIL', self.scan('let web = Other()\nweb.load(request)')['status'])
        self.assertEqual('NOT_VERIFIABLE', self.scan('let web = WKWebView()\nweb.navigationDelegate = self')['status'])

    def test_source_incomplete_only_blocks_absence(self):
        self.assertEqual('NOT_VERIFIABLE', self.scan('', incomplete=True)['status'])
        self.assertEqual('PASS', self.scan('let web = WKWebView()\nweb.load(request)', incomplete=True)['status'])

    def test_global_result_has_no_invented_protocol_url_or_entry(self):
        result = self.scan('func analytics(web: WKWebView) { web.load(request) }')
        self.assertEqual('PASS', result['status'])
        for key in ('kind', 'url', 'missing'):
            self.assertNotIn(key, result)
        self.assertTrue(result['evidence'])

    def test_closure_parameter_shadow_and_reassignment_do_not_borrow_wk(self):
        for source in (
            'let web = WKWebView(); other.forEach { web in web.load(request) }',
            'var web: AnyObject = WKWebView(); web = Other(); web.load(request)',
        ):
            self.assertEqual('NOT_VERIFIABLE', self.scan(source)['status'])
        self.assertEqual('PASS', self.scan('var web: AnyObject = WKWebView(); web.load(request)')['status'])

    def test_explicit_wk_type_does_not_require_initializer_resolution(self):
        source = 'class A { var web: WKWebView!; func f() { web = makeWeb(); web.load(request) } }'
        self.assertEqual('PASS', self.scan(source)['status'])
