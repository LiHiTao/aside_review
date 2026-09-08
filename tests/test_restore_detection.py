import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from restore_detection import detect_restore


class RestoreDetectionTests(unittest.TestCase):
    def detect(self, text, suffix=".swift"):
        return detect_restore(Path("Example" + suffix), text)

    def test_agreement_prose_is_not_functionality(self):
        for suffix in (".md", ".html", ".txt"):
            self.assertEqual([], self.detect("This app does not provide Restore Purchases. 恢复购买功能不受支持。", suffix))

    def test_embedded_agreement_and_api_examples_are_not_code(self):
        source = '''let terms = #"""
This app does not provide Restore Purchases.
We do not call AppStore.sync() or restorePurchases().
"""#
let explanation = "restoreCompletedTransactions() is unsupported"
'''
        self.assertEqual([], self.detect(source))

    def test_negative_prose_cannot_hide_same_line_api(self):
        source = 'let terms = "No Restore Purchases"; try await AppStore.sync()'
        hits = self.detect(source)
        self.assertEqual(1, len(hits))
        self.assertEqual(1, hits[0]["line"])

    def test_real_apis_and_declarations(self):
        for source in (
            'SKPaymentQueue.default().restoreCompletedTransactions()',
            'try await AppStore.sync()',
            'func restorePurchases() async {}',
            'store.restorePurchases(completion: nil)',
        ):
            self.assertTrue(self.detect(source), source)
        self.assertTrue(self.detect('[[SKPaymentQueue defaultQueue] restoreCompletedTransactions];', '.m'))
        self.assertTrue(self.detect('- (void)restorePurchases { }', '.m'))

    def test_comments_backup_and_unrelated_sync_ignored(self):
        source = '''// restorePurchases()
/* AppStore.sync() /* nested */ restoreCompletedTransactions() */
backup.restore()
store.sync()
let copy = "Restore backup"
'''
        self.assertEqual([], self.detect(source))

    def test_restore_controls(self):
        for source in (
            'Button("Restore Purchases") { restore() }',
            'Button("Restore Purchases", action: recover)',
            'Button { recover() } label: { Label("Restore Purchases", systemImage: "arrow.clockwise") }',
            'Button { recover() } label: { Text("Restore Purchases") }',
            'Button(action: { recover() }) { Text("Restore Purchases") }',
            'button.setTitle("Restore Purchases", for: .normal)',
            '[button setTitle:@"Restore Purchases" forState:UIControlStateNormal];',
        ):
            self.assertTrue(self.detect(source, '.m' if source.startswith('[') else '.swift'), source)

    def test_multiline_ui_label_and_accurate_lines(self):
        source = '\nButton(\n"Restore Purchases"\n) { recover() }'
        self.assertEqual(3, self.detect(source)[0]['line'])

    def test_localization_values_not_keys(self):
        self.assertEqual([], self.detect('"restorePurchases" = "This app does not provide Restore Purchases.";', '.strings'))
        self.assertTrue(self.detect('"purchase_restore" = "Restore Purchases";', '.strings'))
        self.assertTrue(self.detect('{"strings":{"restore":{"localizations":{"en":{"stringUnit":{"value":"Restore Purchases","state":"translated"}}}}}}', '.xcstrings'))

    def test_escaped_unicode_xcstrings_values(self):
        document = {"strings": {"restore": {"localizations": {"zh": {"stringUnit": {"value": "恢复购买"}}}}}}
        self.assertTrue(self.detect(json.dumps(document), '.xcstrings'))

    def test_storyboard_controls_not_prose_labels(self):
        self.assertEqual([], self.detect('<document><label text="Restore Purchases"/></document>', '.storyboard'))
        self.assertEqual([], self.detect('<document><!-- <button title="Restore Purchases"/> --></document>', '.storyboard'))
        self.assertTrue(self.detect('<document><button><state key="normal" title="Restore Purchases"/></button></document>', '.storyboard'))
        self.assertTrue(self.detect('<document><barButtonItem title="恢复购买"/></document>', '.xib'))


if __name__ == '__main__':
    unittest.main()
