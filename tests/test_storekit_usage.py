from pathlib import Path
import tempfile
import unittest
from scripts.storekit_usage import analyze_storekit_usage
from scripts.audit_ios_a_side import Auditor, RULE_ORDER

class StoreKitUsageTests(unittest.TestCase):
    def scan(self, source, suffix='.swift', incomplete=False, extra=None):
        sources = {Path('/project/Main' + suffix): source}
        sources.update({Path('/project') / p: s for p, s in (extra or {}).items()})
        return analyze_storekit_usage(Path('/project'), sources, scan_incomplete=incomplete)

    def test_modern_api_variants(self):
        for api in ['Product.products(for: ids)', 'Transaction.updates', 'Transaction.currentEntitlements',
                    'Transaction.unfinished', 'Transaction.all', 'Transaction.latest(for: id)',
                    'Transaction.latest(for: "com.app.one")',
                    'ProductView(id: id)', 'StoreView(ids: ids)', 'SubscriptionStoreView(groupID: id)']:
            for source in ['import StoreKit\n' + api, 'StoreKit.' + api, 'import StoreKit\nfunc wrapper() { '+api+' }']:
                with self.subTest(source=source):
                    result = self.scan(source)
                    self.assertEqual('PASS', result['status'], result)
                    self.assertEqual('Main.swift', result['evidence'][0]['path'])

    def test_explicit_product_purchase(self):
        for source in ['func buy(product: Product) { try await product.purchase() }',
                       'class Shop { var product: Product?\nfunc buy() { product?.purchase() } }',
                       'let product: StoreKit.Product = item\nproduct.purchase()',
                       'items.forEach { (product: Product) in product.purchase() }']:
            self.assertEqual('PASS', self.scan('import StoreKit\n' + source)['status'])

    def test_formatting_and_evidence(self):
        result = self.scan('import StoreKit\nlet p = try await StoreKit . Product\n . products (\n for : ids)')
        self.assertEqual('PASS', result['status'])
        self.assertEqual(2, result['evidence'][0]['line'])

    def test_no_evidence(self):
        for source in ['import StoreKit', 'import StoreKit\nvar p: Product', 'p.purchase()',
                       'import StoreKit\nAppStore.sync()', 'Product.products(for: ids)',
                       'import StoreKit\n// Product.products(for: ids)',
                       'import StoreKit\nlet example = "Transaction.updates"',
                       'import StoreKit\nlet example = #"Product.products(for: ids)"#',
                       'import StoreKit\n/* StoreView(ids: ids) */',
                       'import StoreKit\nlet products: [Product] = []\nproducts.purchase()',
                       'import StoreKit\nfoo.Product.products(for: ids)',
                       'import StoreKit\nfoo.StoreKit.Product.products(for: ids)',
                       'import StoreKit\nCustom . Transaction.updates',
                       'import StoreKit\nlet reference = Product.products(for:)',
                       'import StoreKit\nlet reference = Transaction.latest(for:)',
                       'import StoreKit\nxs.forEach { Transaction in Transaction.updates }',
                       'import StoreKit\nSKPaymentQueue.default().add(observer)',
                       'import StoreKit\nclass C { var item: Other; func f(item: Product) { self.item.purchase() } }',
                       'import StoreKit\nclass C { var p: Other; func f(p: Product){}; func g(){p.purchase()} }',
                       'import StoreKit\nfunc f(p:Product){ others.forEach { p in p.purchase() } }',
                       'import StoreKit\nfunc f<Product>(_ p:Product){p.purchase()}',
                       'import StoreKit\nstruct C<Product> {func f(p:Product){p.purchase()}}',
                       'import StoreKit\nfunc f(p:Product){ xs.forEach {\n p in\n p.purchase()\n }}',
                       'import StoreKit\nfunc f(p:Product){ other . p.purchase() }']:
            with self.subTest(source=source):
                self.assertEqual('NOT_VERIFIABLE', self.scan(source)['status'])

    def test_shadowed_symbols(self):
        for source in ['struct Product {}\nProduct.products(for: ids)',
                       'let Transaction = Other()\nTransaction.updates',
                       'func f(StoreView: Factory) { StoreView(ids: ids) }',
                       'class ProductView {}\nProductView(id: id)',
                       'let StoreKit = Other()\nStoreKit.Product.products(for: ids)',
                       'func buy(product: Product) { let product = Other()\nproduct.purchase() }']:
            with self.subTest(source=source):
                self.assertEqual('NOT_VERIFIABLE', self.scan('import StoreKit\n' + source)['status'])
        self.assertEqual('NOT_VERIFIABLE', self.scan('import StoreKit\nProduct.products(for: ids)', extra={'Local.swift':'struct Product {}'})['status'])
        self.assertEqual('PASS', self.scan('struct Product {}\nStoreKit.Product.products(for: ids)')['status'])

    def test_only_legacy_and_mixed(self):
        legacy = 'import StoreKit\nlet payment = SKPayment(product: item)\nSKPaymentQueue.default().add(payment)'
        self.assertEqual('FAIL', self.scan(legacy)['status'])
        self.assertEqual('NOT_VERIFIABLE', self.scan(legacy, incomplete=True)['status'])
        self.assertEqual('PASS', self.scan(legacy+'\n_ = Transaction.updates', incomplete=True)['status'])
        self.assertEqual('FAIL', self.scan('#import <StoreKit/StoreKit.h>\n[[SKPaymentQueue defaultQueue] addPayment:payment];', '.m')['status'])
        self.assertEqual('PASS', self.scan('@import StoreKit;\n[[SKPaymentQueue defaultQueue] addPayment:payment];', '.m', extra={'Shop.swift':'import StoreKit\n_ = Transaction.updates'})['status'])

    def test_integration_summary_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'Shop.swift').write_text('import StoreKit\nProduct.products(for: ids)')
            report = Auditor(root).run().report()
            summary = next(f for f in report['findings'] if f['id']=='IAP-SUMMARY')
            detail = next(f for f in summary['details'] if f['id']=='IAP-010')
            self.assertEqual('PASS', detail['status'])
            self.assertEqual(17, len(RULE_ORDER))
            self.assertIn('完整交易流程', detail['actual'])
