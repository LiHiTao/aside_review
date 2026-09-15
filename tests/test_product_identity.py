import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.audit_ios_a_side import Auditor, DEFAULT_POLICY


class ProductIdentityTests(unittest.TestCase):
    def scan(self, source, ids=None):
        with tempfile.TemporaryDirectory(prefix='aside-product-identity-') as directory:
            root = Path(directory)
            (root / 'Products.swift').write_text(source)
            products = [{'product_id': pid, 'price_usd': price, 'submit_for_review': True}
                        for pid, price in (ids or [('com.huvex.memos1', '0.99')])]
            (root / 'app_store_config.json').write_text(json.dumps({'iap_products': products}))
            auditor = Auditor(root, DEFAULT_POLICY).run()
            checks = next(f for f in auditor.findings if f['id'] == 'IAP-SUMMARY')['details']
            return auditor, {f['id']: f for f in checks}

    def test_seven_memo_tiers_are_seven_products(self):
        prices = ['0.99', '2.99', '4.99', '9.99', '19.99', '49.99', '99.99']
        ids = [(f'com.huvex.memos{i}', price) for i, price in enumerate(prices, 1)]
        source = 'static let tiers: [MemoProductTier] = [\n' + ',\n'.join(
            f'MemoProductTier(id: "{i}", productId: "{pid}", memos: 40, displayPrice: "${price}", storeTitle: "Starter Pack", sortPrice: {price})'
            for i, (pid, price) in enumerate(ids, 1)) + '\n].sorted { $0.sortPrice < $1.sortPrice }'
        auditor, checks = self.scan(source, ids)
        self.assertEqual([p.product_id for p in auditor.code_products], [p for p, _ in ids])
        self.assertEqual(checks['IAP-002']['status'], 'PASS')

    def test_explicit_alias_order_and_case(self):
        for alias in ['productId', 'productID', 'product_id', 'PRODUCTID', 'PRODUCT_ID']:
            for fields in [f'id: "1", {alias}: "com.huvex.memos1"', f'{alias}: "com.huvex.memos1", id: "1"']:
                with self.subTest(fields=fields):
                    auditor, checks = self.scan(f'MemoProductTier({fields})')
                    self.assertEqual([p.product_id for p in auditor.code_products], ['com.huvex.memos1'])
                    self.assertEqual(checks['IAP-002']['status'], 'PASS')

    def test_nested_calls_and_parentheses_in_strings_preserve_scope(self):
        source = 'MemoProductTier(id: "1", title: "A ) ( B", decoration: wrap(other(1)), productId: "com.huvex.memos1")'
        auditor, checks = self.scan(source)
        self.assertEqual(len(auditor.code_products), 1)
        self.assertEqual(checks['IAP-002']['status'], 'PASS')

    def test_adjacent_and_nested_generic_records_remain_detected(self):
        for extra in ['StoreProduct(id: "com.extra.SKU")', '(id: "com.extra.SKU", price: "$1.99", coins: 20)']:
            for source in [f'let a = Tier(id: "1", productId: "com.huvex.memos1"); let b = {extra}',
                           f'Tier(id: "1", productId: "com.huvex.memos1", child: {extra})',
                           f'StoreProduct(id: "com.extra.SKU", child: Tier(productId: "com.huvex.memos1"))']:
                with self.subTest(source=source):
                    auditor, checks = self.scan(source)
                    self.assertEqual({p.product_id for p in auditor.code_products}, {'com.huvex.memos1', 'com.extra.SKU'})
                    self.assertEqual(checks['IAP-002']['status'], 'FAIL')
                    self.assertEqual(checks['IAP-006']['status'], 'FAIL')

    def test_comments_and_literal_mentions_are_not_fields(self):
        for note in ['/* productId: "ignored" */', 'note: #"productId: "ignored""#,', 'note: "productId: dynamicValue",']:
            auditor, checks = self.scan(f'StoreProduct({note} id: "com.huvex.memos1")')
            self.assertEqual([p.product_id for p in auditor.code_products], ['com.huvex.memos1'])
            self.assertEqual(auditor.dynamic_code_products, [])
            self.assertEqual(checks['IAP-002']['status'], 'PASS')

    def test_dynamic_explicit_never_falls_back_to_local_id(self):
        for value in ['remote.productID', 'makeIdentifier(prefix: "com.huvex")', '"com.huvex." + suffix', '"com.huvex.\\(number)"']:
            with self.subTest(value=value):
                auditor, checks = self.scan(f'MemoProductTier(id: "1", productId: {value})')
                self.assertEqual(auditor.code_products, [])
                self.assertEqual(len(auditor.dynamic_code_products), 1)
                self.assertEqual(checks['IAP-002']['status'], 'NOT_VERIFIABLE')

    def test_known_generic_id_in_separate_record_is_kept(self):
        auditor, checks = self.scan('Tier(id: "1", productID: "com.other.product"), Notice(id: "com.huvex.memos1")', [('com.other.product', '0.99'), ('com.huvex.memos1', '2.99')])
        self.assertEqual(len(auditor.code_products), 2)
        self.assertEqual(checks['IAP-002']['status'], 'PASS')

    def test_property_and_parameter_types_are_not_dynamic_catalogue_entries(self):
        source = ('struct MemoProductTier { let productId: String; var id: String\n'
                  'init(id: String, productId: String) { self.id = id; self.productId = productId }\n'
                  'func configure(productId: String) {} }\n'
                  'MemoProductTier(id: "1", productId: "com.huvex.memos1")')
        auditor, checks = self.scan(source)
        self.assertEqual([p.product_id for p in auditor.code_products], ['com.huvex.memos1'])
        self.assertEqual(auditor.dynamic_code_products, [])
        self.assertEqual(checks['IAP-002']['status'], 'PASS')

    def test_generic_declarations_are_not_catalogue_entries(self):
        source = ('func configure<T>(productId: String, value: T) {}\n'
                  'func configureNested<T: Collection<Array<String>>>(productID: String, value: T) {}\n'
                  'struct Model { init<T>(product_id: String, value: T) {} }\n'
                  'MemoProductTier<String>(id: "1", productId: "com.huvex.memos1")')
        auditor, checks = self.scan(source)
        self.assertEqual([p.product_id for p in auditor.code_products], ['com.huvex.memos1'])
        self.assertEqual(auditor.dynamic_code_products, [])
        self.assertEqual(checks['IAP-002']['status'], 'PASS')

    def test_generic_product_initializers_keep_identity_semantics(self):
        auditor, checks = self.scan('StoreProduct<Array<String>>(id: "com.extra.SKU")')
        self.assertEqual([p.product_id for p in auditor.code_products], ['com.extra.SKU'])
        self.assertEqual(checks['IAP-002']['status'], 'FAIL')
        auditor, checks = self.scan('MemoProductTier<String>(id: "1", productId: makeID())')
        self.assertEqual(auditor.code_products, [])
        self.assertEqual(len(auditor.dynamic_code_products), 1)
        self.assertEqual(checks['IAP-002']['status'], 'NOT_VERIFIABLE')

    def test_wrong_explicit_id_remains_failure(self):
        auditor, checks = self.scan('MemoProductTier(id: "com.huvex.memos1", productID: "com.wrong.SKU")')
        self.assertEqual([p.product_id for p in auditor.code_products], ['com.wrong.SKU'])
        self.assertEqual(checks['IAP-002']['status'], 'FAIL')
        self.assertEqual(checks['IAP-006']['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()

class ProductInitializerRegressionTests(unittest.TestCase):
    scan = ProductIdentityTests.scan
    def test_init_call_forms_and_type_inference(self):
        for call in ['.init', '. init', 'MemoProductTier.init', 'MemoProductTier. init', 'MemoProductTier<String>.init']:
            with self.subTest(call=call):
                auditor, checks = self.scan('let tiers: [MemoProductTier] = [' + call + '(id: "com.extra.SKU")]')
                self.assertEqual([p.product_id for p in auditor.code_products], ['com.extra.SKU'])
                self.assertEqual(checks['IAP-002']['status'], 'FAIL')
                auditor, checks = self.scan('let tiers: [MemoProductTier] = [' + call + '(id: "1", productID: "com.huvex.memos1")]')
                self.assertEqual([p.product_id for p in auditor.code_products], ['com.huvex.memos1'])
                self.assertEqual(auditor.dynamic_code_products, [])

    def test_business_init_and_nested_init_do_not_inherit_product_type(self):
        for source in ['let rows: [Notice] = [.init(id: "plain.row")]',
                       'let tiers: [MemoProductTier] = [.init(productID: "com.huvex.memos1", badge: .init(id: "plain.row"))]']:
            auditor, _ = self.scan(source)
            self.assertNotIn('plain.row', [p.product_id for p in auditor.code_products])

    def test_foreach_static_catalogue_reference(self):
        auditor, checks = self.scan('''let catalog: [StoreProduct] = [.init(id: "com.huvex.memos1")]
        ForEach(catalog) { product in Button("Buy") { purchase(productID: product.id) } }''')
        self.assertEqual(auditor.dynamic_code_products, [])
        self.assertEqual(checks['IAP-002']['status'], 'PASS')

    def test_foreach_unresolved_sources_and_shadowing(self):
        catalog = 'let catalog: [StoreProduct] = [.init(id: "com.huvex.memos1")]\n'
        for body in ['let product = remote; purchase(productID: product.id)',
                     'product = remote; purchase(productID: product.id)',
                     'product.id = remoteID; purchase(productID: product.id)',
                     'purchase(productID: unknown.id)',
                     'purchase(productID: product.id + suffix)']:
            auditor, checks = self.scan(catalog + 'ForEach(catalog) { product in ' + body + ' }')
            self.assertTrue(auditor.dynamic_code_products, body)
            self.assertEqual(checks['IAP-002']['status'], 'NOT_VERIFIABLE')
        for prefix in ['let catalog = remote\n', '']:
            source = catalog + 'func other() { ' + prefix + 'ForEach(remoteCatalog) { product in purchase(productID: product.id) } }'
            auditor, _ = self.scan(source)
            self.assertTrue(auditor.dynamic_code_products)
        auditor, _ = self.scan(catalog + 'func other() { let catalog = remote; ForEach(catalog) { product in purchase(productID: product.id) } }')
        self.assertTrue(auditor.dynamic_code_products)

    def test_explicit_identity_not_local_sequence_reference(self):
        auditor, _ = self.scan('''let catalog: [StoreProduct] = [.init(id: "1", productID: "com.huvex.memos1")]
        ForEach(catalog) { product in purchase(productID: product.id) }''')
        self.assertTrue(auditor.dynamic_code_products)

    def test_partial_dynamic_catalogue_stays_unknown(self):
        auditor, checks = self.scan('''let catalog: [StoreProduct] = [.init(id: "com.huvex.memos1"), .init(id: remote)]
        ForEach(catalog) { product in purchase(productID: product.id) }''')
        self.assertEqual(len(auditor.dynamic_code_products), 2)
        self.assertEqual(checks['IAP-002']['status'], 'NOT_VERIFIABLE')

    def test_typed_transaction_read_is_not_new_product_but_purchase_is(self):
        source = 'func completed(transaction: SKPaymentTransaction) { let productID = transaction.payment.productIdentifier }'
        auditor, _ = self.scan(source)
        self.assertEqual(auditor.dynamic_code_products, [])
        for source in ['func completed(transaction: OtherTransaction) { let productID = transaction.payment.productIdentifier }',
                       'func completed(transaction: SKPaymentTransaction) { purchase(productID: transaction.payment.productIdentifier) }',
                       'func completed(transaction: SKPaymentTransaction) { StoreProduct(productID: transaction.payment.productIdentifier) }']:
            auditor, _ = self.scan(source)
            self.assertEqual(len(auditor.dynamic_code_products), 1)

    def test_cross_file_qualified_catalogue(self):
        with tempfile.TemporaryDirectory(prefix='aside-product-reference-') as directory:
            root = Path(directory)
            (root / 'Catalog.swift').write_text('enum Catalog { static let tiers: [StoreProduct] = [.init(productID: "com.example.sku")] }')
            (root / 'View.swift').write_text('ForEach(Catalog.tiers) { product in purchase(productID: product.productID) }')
            auditor = Auditor(root).run()
            self.assertEqual([p.product_id for p in auditor.code_products], ['com.example.sku'])
            self.assertEqual(auditor.dynamic_code_products, [])

    def test_duplicate_type_cannot_lend_catalogue(self):
        auditor, _ = self.scan('''enum Catalog { static let tiers: [StoreProduct] = [.init(id: "com.huvex.memos1")] }
        struct Other { enum Catalog { static let tiers = remote } }
        ForEach(Catalog.tiers) { product in purchase(productID: product.id) }''')
        self.assertTrue(auditor.dynamic_code_products)

    def test_nested_catalogue_not_visible_outside_function(self):
        auditor, _ = self.scan('''func setup() { let catalog: [StoreProduct] = [.init(id: "com.huvex.memos1")] }
        ForEach(catalog) { product in purchase(productID: product.id) }''')
        self.assertTrue(auditor.dynamic_code_products)

    def test_known_catalogue_does_not_hide_other_unknown_purchase(self):
        auditor, _ = self.scan('''let catalog: [StoreProduct] = [.init(id: "com.huvex.memos1")]
        ForEach(catalog) { product in purchase(productID: product.id) }
        purchase(productID: remoteID)''')
        self.assertEqual(len(auditor.dynamic_code_products), 1)

    def test_function_and_closure_parameter_shadowing(self):
        base = 'let catalog: [StoreProduct] = [.init(id: "com.huvex.memos1")]\n'
        sources = [
            'func view(catalog: [StoreProduct]) { ForEach(catalog) { product in purchase(productID: product.id) } }',
            'remote.withCatalog { catalog in ForEach(catalog) { product in purchase(productID: product.id) } }',
            'ForEach(catalog) { product in func buy(product: StoreProduct) { purchase(productID: product.id) } }',
            'ForEach(catalog) { product in other { (product: StoreProduct) in purchase(productID: product.id) } }',
            'ForEach(catalog) { product in other { [weak self] (product: StoreProduct) in purchase(productID: product.id) } }',
            'ForEach(catalog) { product in func buy<T>(product: T) { purchase(productID: product.id) } }',
            'ForEach(catalog) { product in for product in remote { purchase(productID: product.id) } }',
        ]
        for source in sources:
            with self.subTest(source=source):
                auditor, _ = self.scan(base + source)
                self.assertTrue(auditor.dynamic_code_products)
        auditor, _ = self.scan('''enum Catalog { static let tiers: [StoreProduct] = [.init(id: "com.huvex.memos1")] }
        func view(Catalog: Provider) { ForEach(Catalog.tiers) { product in purchase(productID: product.id) } }''')
        self.assertTrue(auditor.dynamic_code_products)

    def test_catalogue_transformations_are_unresolved(self):
        for suffix in ['.map { _ in remote }', ' + remoteProducts', '.replacingProducts()', '\n.map { _ in remote }']:
            auditor, _ = self.scan('let catalog: [StoreProduct] = [.init(id: "com.huvex.memos1")]' + suffix + '\nForEach(catalog) { product in purchase(productID: product.id) }')
            self.assertTrue(auditor.dynamic_code_products, suffix)

    def test_transaction_parameter_shadowing_is_unresolved(self):
        for body in ['func other(transaction: Other) { let productID = transaction.payment.productIdentifier }',
                     'remote { transaction in let productID = transaction.payment.productIdentifier }']:
            auditor, _ = self.scan('func done(transaction: SKPaymentTransaction) { ' + body + ' }')
            self.assertTrue(auditor.dynamic_code_products, body)

    def test_nested_child_product_cannot_define_parent_identity(self):
        auditor, _ = self.scan('''let catalog = [Notice(child: StoreProduct(id: "com.huvex.memos1"))]
        ForEach(catalog) { product in purchase(productID: product.id) }''')
        self.assertTrue(auditor.dynamic_code_products)

    def test_nested_child_identity_label_cannot_lend_to_parent(self):
        auditor, _ = self.scan('''let catalog = [StoreProduct(id: "com.huvex.memos1", child: StoreProduct(productID: "com.huvex.memos1"))]
        ForEach(catalog) { product in purchase(productID: product.productID) }''')
        self.assertTrue(auditor.dynamic_code_products)
