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
