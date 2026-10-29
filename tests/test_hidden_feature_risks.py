from pathlib import Path
import tempfile
import unittest

from scripts.hidden_feature_risks import analyze_hidden_risks
from scripts.audit_ios_a_side import Auditor, RULE_ORDER


def config(debug=False):
    return 'x = { isa = XCBuildConfiguration; buildSettings = { SWIFT_ACTIVE_COMPILATION_CONDITIONS = "' + ('DEBUG' if debug else '') + '"; }; name = Release; };'


class HiddenRiskTests(unittest.TestCase):
    def scan(self, source, release=False, extras=None, incomplete=False):
        sources = {Path('/project/Main.swift'): source}
        sources.update({Path('/project') / name: raw for name, raw in (extras or {}).items()})
        texts = dict(sources)
        if release is not None: texts[Path('/project/App.xcodeproj/project.pbxproj')] = config(release)
        return analyze_hidden_risks(Path('/project'), sources, texts, scan_incomplete=incomplete)

    def labels(self, result):
        return {d['label'] for d in result['details']}

    def test_review_condition_encloses_routing(self):
        result = self.scan('func boot() { if isReview { window.rootViewController = ToolController() } }')
        self.assertEqual(result['status'], 'WARN')
        self.assertEqual(result['severity'], 'high')
        self.assertIn('隐藏业务切换', self.labels(result))
        for source in ('func boot() { if reviewRequested { SKStoreReviewController.requestReview() } }',
                       'func boot() { if remoteEnabled { setTheme() } }',
                       'func review() { let isReview = true }; func boot() { window.rootViewController = Tool() }'):
            self.assertEqual(self.scan(source)['status'], 'PASS')

    def test_objc_review_routing(self):
        root = Path('/project')
        source = '@implementation App\n -(void)boot { if (self.isReview) { self.window.rootViewController = [[BusinessController alloc] init]; } }\n@end'
        path = root / 'App.m'
        result = analyze_hidden_risks(root, {path: source}, {})
        self.assertEqual(result['status'], 'WARN')
        self.assertIn('隐藏业务切换', self.labels(result))

    def test_debug_config_and_else_nested(self):
        source = 'func boot() {\n#if DEBUG\n if isReview { window.rootViewController = Tool() }\n#else\n showWelcome()\n#endif\n}'
        self.assertEqual(self.scan(source)['status'], 'PASS')
        self.assertEqual(self.scan(source, release=True)['status'], 'WARN')
        self.assertEqual(self.scan(source, release=None)['status'], 'NOT_VERIFIABLE')
        other = source.replace('showWelcome()', 'if isReview { window.rootViewController = Business() }')
        self.assertEqual(self.scan(other)['status'], 'WARN')
        nested = 'func boot() {\n#if DEBUG\n#if EXPERIMENT\nif isReview { window.rootViewController = Tool() }\n#endif\n#endif\n}'
        self.assertEqual(self.scan(nested)['status'], 'PASS')

    def test_unresolved_release_settings_cannot_exclude_debug(self):
        source = 'func boot() {\n#if DEBUG\n if isReview { window.rootViewController = Tool() }\n#endif\n}'
        root = Path('/project')
        path = root / 'Main.swift'
        inherited = config().replace('""', '"$(inherited)"')
        self.assertEqual(analyze_hidden_risks(root, {path: source}, {root / 'App/project.pbxproj': inherited})['status'], 'NOT_VERIFIABLE')
        dual = config().replace('name = Release;', 'name = Release;').replace('SWIFT_ACTIVE_COMPILATION_CONDITIONS = "";', 'SWIFT_ACTIVE_COMPILATION_CONDITIONS = ""; GCC_PREPROCESSOR_DEFINITIONS = "DEBUG=1";')
        self.assertEqual(analyze_hidden_risks(root, {path: source}, {root / 'App/project.pbxproj': dual})['status'], 'WARN')

    def test_demo_record_to_store(self):
        source = 'func seedDemo() { let item = DemoEntry(id: "sample"); storage.upsert(item) }'
        result = self.scan(source)
        self.assertEqual(result['status'], 'WARN')
        self.assertIn('演示内容写入业务存储', self.labels(result))
        self.assertGreaterEqual(len(result['details'][0]['evidence']), 1)
        self.assertEqual(self.scan('func seedCatalog() { return [Bird(name: "Robin")] }')['status'], 'PASS')
        self.assertEqual(self.scan('func seedDemo() { return [DemoEntry(id: "sample")] }; func saveOther() { store.save(real) }')['status'], 'PASS')
        self.assertEqual(self.scan('func seedPreview() { let preview = SampleRecord(title: "Demo"); store.save(userRecord) }')['status'], 'PASS')

    def test_provider_consumer_assignment_and_save(self):
        source = '''enum DemoShelf { static func records() { return [DemoEntry(id: "sample")] } }
        class Store { func boot() { let values = DemoShelf.records(); entries = values; save() } }'''
        self.assertIn('演示内容写入业务存储', self.labels(self.scan(source)))
        guarded = source.replace('enum DemoShelf', '#if EXPERIMENT\nenum DemoShelf').replace('\n        class Store', '\n#endif\n        class Store')
        self.assertEqual(self.scan(guarded)['status'], 'NOT_VERIFIABLE')
        unrelated = source.replace('entries = values; save()', 'let preview = values; settings.save(userSettings)')
        self.assertEqual(self.scan(unrelated)['status'], 'PASS')
        self.assertEqual(self.scan(source.replace('save()', 'settings.save(userSettings)'))['status'], 'PASS')

    def test_asset_fallback_requires_display_and_avoids_placeholder(self):
        source = 'func share() { let poster = image ?? UIImage(named: "TravelPoster"); draw(poster, in: rect) }'
        self.assertIn('素材替代用户内容', self.labels(self.scan(source)))
        self.assertEqual(self.scan(source.replace('TravelPoster', 'ImagePlaceholder'))['status'], 'PASS')
        self.assertEqual(self.scan('func share() { let poster = image ?? UIImage(named: "TravelPoster") }; func other() { draw(real) }')['status'], 'PASS')
        self.assertEqual(self.scan('func share() { let demo = poster ?? UIImage(named: "VacationStock"); userImage.draw(in: rect) }')['status'], 'PASS')

    def test_progress_has_timer_helper_and_terminal_work(self):
        source = '''class Screen {
        func start() { Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { tick($0) } }
        func tick(_ timer: Timer) { progress = min(progress + 2.4, 100); if progress >= 100 { timer.invalidate(); finishRender() } }
        }'''
        self.assertIn('模拟业务进度', self.labels(self.scan(source)))
        self.assertEqual(self.scan(source.replace('progress = min(progress + 2.4, 100)', 'progress = exportSession.progress'))['status'], 'PASS')
        self.assertEqual(self.scan('func tick() { progress += 1 }; func timer() { Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { clock() } }')['status'], 'PASS')

    def test_local_only_success_explicit_branch(self):
        source = 'func publish() { store.publishLocal(post); guard remoteEnabled else { completion(.success(post)); return }; send(post) }'
        self.assertIn('本地结果作为成功返回', self.labels(self.scan(source)))
        self.assertEqual(self.scan('func localSave() { store.save(item); completion(.success(item)) }')['status'], 'PASS')

    def test_keywords_comments_strings_webviews_and_defaults_do_not_fail(self):
        for source in ('// B side review mock seed\nfunc normal() { let value = 12.4 }',
                       'let prose = "if isReview { window.rootViewController = Hidden() }"',
                       'func legal() { let web = WKWebView(); web.load(request); let word = "base64" }',
                       'func clock() { Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { updateClock() } }'):
            self.assertEqual(self.scan(source)['status'], 'PASS')
        self.assertEqual(self.scan('func normal() {}', incomplete=True)['status'], 'NOT_VERIFIABLE')

    def test_integration_twenty_checks_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'App.swift').write_text('func boot() { if isReview { window.rootViewController = Tool() } }')
            report = Auditor(root).run().report()
            self.assertEqual(len(RULE_ORDER), 20)
            self.assertEqual(report['schema_version'], '2.0')
            finding = next(f for f in report['findings'] if f['id'] == 'REVIEW-001')
            self.assertEqual(finding['status'], 'WARN')
            self.assertIn('不证明', finding['actual'])


if __name__ == '__main__':
    unittest.main()
