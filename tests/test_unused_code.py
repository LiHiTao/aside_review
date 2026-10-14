import tempfile
import unittest
from pathlib import Path

from scripts.audit_ios_a_side import Auditor, RULE_ORDER
from scripts.unused_code import audit_unused_code


class UnusedCodeTests(unittest.TestCase):
    def check(self, raw, policy=None, extra=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'Business.swift'
            path.write_text(raw)
            sources = {path: raw}
            texts = dict(sources)
            for name, content in (extra or {}).items():
                other = root / name
                other.parent.mkdir(parents=True, exist_ok=True)
                other.write_text(content)
                texts[other] = content
                if other.suffix in {'.swift', '.m'}:
                    sources[other] = content
            result = audit_unused_code(root, policy or {}, sources, texts)
            self.assertEqual(path.read_text(), raw)
            return result

    def metrics(self, result):
        return result['details'][0]

    def test_recursive_only_and_effective_physical_lines(self):
        result = self.check('private func unused() {\n // ignore\n\n unused()\n}\nlet live = 1\n')
        self.assertEqual(self.metrics(result)['suspected_lines'], 3)
        self.assertEqual(self.metrics(result)['total_code_lines'], 4)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['details'][1]['line'], 1)
        self.assertEqual(result['details'][1]['end_line'], 5)

    def test_external_reference_and_name_collisions_retain(self):
        base = 'private func helper() {\n let value = 1\n}\n'
        for tail in ('helper()\n', 'let reference = helper\n', 'func helper() {}\n', 'let value = "helper"\n'):
            with self.subTest(tail=tail):
                result = self.check(base + tail)
                self.assertEqual(self.metrics(result)['candidate_count'], 0)
        result = self.check(base, extra={'Other.swift': 'func live() { helper() }'})
        self.assertEqual(self.metrics(result)['candidate_count'], 0)

    def test_comments_do_not_retain_but_selector_resources_do(self):
        base = 'private func helper() {\n let value = 1\n}\n'
        result = self.check(base + '// helper()\n/* helper */\n')
        self.assertEqual(self.metrics(result)['candidate_count'], 1)
        for file in ('Main.storyboard', 'Info.plist'):
            result = self.check(base, extra={file: '<key>helper</key>'})
            self.assertEqual(self.metrics(result)['candidate_count'], 0)

    def test_runtime_attributes_protocols_and_dynamic_files_retained(self):
        examples = [
            '@objc private func helper() {}\n',
            '@IBAction\nprivate func helper() {}\n',
            'private override func helper() {}\n',
            '@objcMembers\nclass Holder {\nprivate func helper() {}\n}\n',
            'class Holder: NSObject {\nprivate func helper() {}\n}\n',
            'struct Holder: Delegate {\nprivate func helper() {}\n}\n',
            'extension Holder {\nprivate func helper() {}\n}\n',
            '#if DEBUG\nprivate func helper() {}\n#endif\n',
            'private func helper() {}\nlet selector = NSSelectorFromString(key)\n',
        ]
        for raw in examples:
            with self.subTest(raw=raw):
                result = self.check(raw)
                if result['status'] == 'NOT_VERIFIABLE':
                    self.assertIn('未获得可分析', result['actual'])
                else:
                    self.assertEqual(self.metrics(result)['candidate_count'], 0)

    def test_explicitbody_and_multiline_modifiers(self):
        result = self.check('private\nstatic func\nhelper(\n value: Int\n) -> Int {\n return value\n}\n')
        self.assertEqual(self.metrics(result)['candidate_count'], 1)
        self.assertEqual(self.metrics(result)['suspected_lines'], 7)
        result = self.check('let example = "private func helper() {}"\n')
        self.assertEqual(self.metrics(result)['candidate_count'], 0)

    def test_ratio_and_absolute_thresholds_both_required(self):
        private = 'private func unused() {\n' + 'let value = 1\n' * 298 + '}\n'
        live = 'let live = 1\n'
        for rest, expected in ((live * 2700, 'WARN'), (live * 2701, 'PASS')):
            result = self.check(private + rest)
            self.assertEqual(result['status'], expected)
        result = self.check('private func unused() {}\n')
        self.assertEqual(result['status'], 'PASS')
        high = self.check('private func unused() {\n' + live * 998 + '}\n' + live * 4000)
        self.assertEqual(high['status'], 'WARN')
        self.assertEqual(high['severity'], 'high')
        self.assertNotEqual(high['status'], 'FAIL')

    def test_policy_validation(self):
        for value in (True, -1, 1.1, float('nan'), '10%'):
            result = self.check('private func helper() {}', {'unused_code_warn_ratio': value})
            self.assertEqual(result['status'], 'NOT_VERIFIABLE')
        for value in (True, -1, 2.5, '300'):
            result = self.check('private func helper() {}', {'unused_code_warn_lines': value})
            self.assertEqual(result['status'], 'NOT_VERIFIABLE')
        self.assertEqual(self.check('private func helper() {}', {'unused_code_high_lines': 1})['status'], 'NOT_VERIFIABLE')

    def test_scope_exclusions_and_incomplete(self):
        result = self.check('let live = 1\n', extra={'Vendor/Dead.swift': 'private func helper() {}'})
        self.assertEqual(self.metrics(result)['candidate_count'], 0)
        self.assertEqual(self.check('')['status'], 'NOT_VERIFIABLE')
        self.assertEqual(self.check('private func helper() {')['status'], 'NOT_VERIFIABLE')
        self.assertEqual(self.check('let raw = "unfinished')['status'], 'NOT_VERIFIABLE')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Legacy.m').write_text('void helper() {}')
            self.assertEqual(audit_unused_code(root, {})['status'], 'NOT_VERIFIABLE')
            (root / 'Code.swift').write_text('private func helper() {}')
            self.assertEqual(audit_unused_code(root, {}, scan_incomplete=True)['status'], 'NOT_VERIFIABLE')

    def test_standalone_resource_refs_and_zero_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Code.swift').write_text('private func helper() {}\n')
            (root / 'Main.storyboard').write_text('<action selector="helper:"/>')
            result = audit_unused_code(root, {})
            self.assertEqual(self.metrics(result)['candidate_count'], 0)
            zero = {key: 0 for key in ('unused_code_warn_ratio', 'unused_code_warn_lines',
                                      'unused_code_high_ratio', 'unused_code_high_lines')}
            self.assertEqual(audit_unused_code(root, zero)['status'], 'PASS')

    def test_integration_schema_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Code.swift').write_text('private func helper() {}\n')
            report = Auditor(root).run().report()
            self.assertEqual(len(RULE_ORDER), 19)
            self.assertEqual(RULE_ORDER[:2], ('CODE-001', 'CODE-002'))
            self.assertEqual(report['schema_version'], '2.0')
            finding = next(item for item in report['findings'] if item['id'] == 'CODE-002')
            self.assertEqual(finding['status'], 'PASS')
            self.assertEqual(finding['details'][0]['candidate_count'], 1)


if __name__ == '__main__':
    unittest.main()
