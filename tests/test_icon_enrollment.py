import hashlib
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from PIL import Image

from scripts.audit_ios_a_side import Auditor, RULE_ORDER
from scripts.icon_audit import audit_app_icons, icon_project_key
from scripts.icon_api import compare_icon, enroll_icons, IconApiError, IconEnrollmentConflict, PIXEL_BYTES


class IconEnrollmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'App'
        self.root.mkdir()

    def icon(self, name, color):
        folder = self.root / 'Assets.xcassets' / f'{name}.appiconset'
        folder.mkdir(parents=True)
        (folder / 'Contents.json').write_text(json.dumps({'images': [
            {'filename': 'icon.png', 'idiom': 'universal', 'platform': 'ios', 'size': '1024x1024'}]}))
        image = folder / 'icon.png'
        Image.new('RGB', (1024, 1024), color).save(image)
        return image

    def audit(self, allowed=True, configs=None):
        return audit_app_icons(self.root, configs or {}, (), enrollment_allowed=allowed)

    def test_two_stage_comparison_then_one_batch(self):
        self.icon('A', 'black'); self.icon('B', 'white')
        configs = {self.root / 'App.xcconfig': 'ASSETCATALOG_COMPILER_APPICON_NAME = A\nASSETCATALOG_COMPILER_APPICON_NAME[sdk=iphoneos*] = B\n'}
        events = []
        def compare(*args):
            events.append('compare')
            return ([{'name': 'Ref', 'score': 70}], False)
        def enroll(*args):
            events.append('enroll')
            self.assertEqual(len(args[0]), 2)
            return [True, False]
        with patch('scripts.icon_audit.compare_icon', side_effect=compare), patch('scripts.icon_audit.enroll_icons', side_effect=enroll):
            finding = self.audit(configs=configs)
        self.assertEqual(events, ['compare', 'compare', 'enroll'])
        self.assertEqual(finding['status'], 'PASS')
        self.assertEqual([row['enrollment_status'] for row in finding['details']], ['confirmed', 'already_enrolled'])
        self.assertEqual([row['added'] for row in finding['details']], [True, False])
        self.assertIn('70.0%', finding['actual'])

    def test_later_high_icon_prevents_enrollment_of_earlier_low_icon(self):
        self.icon('A', 'black'); self.icon('B', 'white')
        configs = {self.root / 'App.xcconfig': 'ASSETCATALOG_COMPILER_APPICON_NAME = A\nASSETCATALOG_COMPILER_APPICON_NAME[sdk=iphoneos*] = B\n'}
        with patch('scripts.icon_audit.compare_icon', side_effect=[([{'name': 'Ref', 'score': 70}], False), ([{'name': 'Other', 'score': 95}], False)]), patch('scripts.icon_audit.enroll_icons') as enroll:
            finding = self.audit(configs=configs)
        self.assertEqual(finding['status'], 'FAIL')
        enroll.assert_not_called()
        self.assertEqual(finding['details'][0]['enrollment_status'], 'skipped')
        self.assertEqual(finding['details'][0]['matches'][0]['score'], 70)

    def test_other_failure_missing_or_failed_comparison_prevents_enrollment(self):
        self.icon('AppIcon', 'black')
        for allowed, error in ((False, None), (True, IconApiError('unavailable'))):
            with self.subTest(allowed=allowed), patch('scripts.icon_audit.compare_icon', side_effect=error, return_value=([{'name': 'Ref', 'score': 70}], False)), patch('scripts.icon_audit.enroll_icons') as enroll:
                finding = self.audit(allowed)
                enroll.assert_not_called()
                self.assertEqual(finding['status'], 'PASS' if not allowed else 'NOT_VERIFIABLE')
        configs = {self.root / 'App.xcconfig': 'ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon\nASSETCATALOG_COMPILER_APPICON_NAME[sdk=iphoneos*] = Missing\n'}
        with patch('scripts.icon_audit.compare_icon', return_value=([{'name': 'Ref', 'score': 70}], False)), patch('scripts.icon_audit.enroll_icons') as enroll:
            self.assertEqual(self.audit(configs=configs)['status'], 'NOT_VERIFIABLE')
            enroll.assert_not_called()

    def test_already_enrolled_and_exact_boundary_do_not_enroll(self):
        self.icon('AppIcon', 'black')
        for score, already, state in ((70, True, 'already_enrolled'), (85, False, 'not_required')):
            with self.subTest(score=score), patch('scripts.icon_audit.compare_icon', return_value=([{'name': 'Ref', 'score': score}], already)), patch('scripts.icon_audit.enroll_icons') as enroll:
                finding = self.audit()
                enroll.assert_not_called()
                self.assertEqual(finding['status'], 'PASS')
                self.assertEqual(finding['details'][0]['enrollment_status'], state)

    def test_uncertain_enrollment_preserves_comparison_score(self):
        self.icon('AppIcon', 'black')
        with patch('scripts.icon_audit.compare_icon', return_value=([{'name': 'Ref', 'score': 70}], False)), patch('scripts.icon_audit.enroll_icons', side_effect=IconApiError('unknown result')):
            finding = self.audit()
        self.assertEqual(finding['status'], 'NOT_VERIFIABLE')
        self.assertEqual(finding['details'][0]['matches'][0]['score'], 70)
        self.assertEqual(finding['details'][0]['enrollment_status'], 'uncertain')

    def test_project_identity_bundle_literal_or_path_fallback(self):
        first = self.root / 'One.xcconfig'
        bundle = 'com.example.App'
        expected = hashlib.sha256(('bundle\0' + bundle).encode()).hexdigest()
        self.assertEqual(icon_project_key(self.root, {first: 'PRODUCT_BUNDLE_IDENTIFIER = com.example.App\n'}), expected)
        second = self.root / 'Info.plist'
        info = plistlib.dumps({'CFBundleIdentifier': bundle}).decode()
        self.assertEqual(icon_project_key(self.root, {second: info}), expected)
        for inputs in ({}, {first: 'PRODUCT_BUNDLE_IDENTIFIER = $(PREFIX).app\n'}, {first: 'PRODUCT_BUNDLE_IDENTIFIER = com.one.App\nPRODUCT_BUNDLE_IDENTIFIER = com.two.App\n'}, {first: '// PRODUCT_BUNDLE_IDENTIFIER = com.example.App\n'}):
            self.assertEqual(icon_project_key(self.root, inputs), hashlib.sha256(('path\0' + str(self.root.resolve())).encode()).hexdigest())
        relocated = self.root / 'Elsewhere'
        self.assertEqual(icon_project_key(relocated, {relocated / 'App.xcconfig': 'PRODUCT_BUNDLE_IDENTIFIER = com.example.App\n'}), expected)

    def test_comparison_and_batch_api_contract(self):
        key = 'a' * 64
        pixels = bytes(PIXEL_BYTES)
        with patch('scripts.icon_api.build_opener') as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({'matches': [{'name': 'Ref', 'score': 70, 'image': 'discard'}], 'added': False, 'already_enrolled': True}).encode()
            self.assertEqual(compare_icon(pixels, 'App Name', key), ([{'name': 'Ref', 'score': 70}], True))
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://app-icon-audit.vercel.app/api/audit/compare')
            self.assertEqual(request.get_header('X-app-project'), key)
            self.assertEqual(request.get_header('X-app-name'), 'App%20Name')
            self.assertIsNone(request.get_header('Authorization'))
            response.read.return_value = b'{"added":[true,false]}'
            self.assertEqual(enroll_icons([pixels, bytes([255]) * PIXEL_BYTES], 'App Name', key), [True, False])
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://app-icon-audit.vercel.app/api/audit/enroll')
            self.assertEqual(request.get_header('X-app-project'), key)
            self.assertIsNone(request.get_header('Authorization'))
            self.assertEqual(request.data, pixels + bytes([255]) * PIXEL_BYTES)

    def test_api_rejects_bad_new_contract_and_batch(self):
        key = 'a' * 64
        pixels = bytes(PIXEL_BYTES)
        with patch('scripts.icon_api.build_opener') as build:
            response = build.return_value.open.return_value.__enter__.return_value
            for payload in ({'matches': [{'name': 'Ref', 'score': 70}], 'added': True, 'already_enrolled': False}, {'matches': [{'name': 'Ref', 'score': 70}], 'added': False}, {'matches': [{'name': 'Ref', 'score': float('nan')}], 'added': False, 'already_enrolled': False}):
                response.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(IconApiError):
                    compare_icon(pixels, 'App', key)
            for payload in ({'added': [True]}, {'added': [True, 0]}, {'added': 'yes'}):
                response.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(IconApiError):
                    enroll_icons([pixels, bytes([255]) * PIXEL_BYTES], 'App', key)
            with self.assertRaises(IconApiError):
                enroll_icons([pixels] * 17, 'App', key)


    def test_enrollment_conflict_is_rejected_with_scores_preserved(self):
        self.icon('AppIcon', 'black')
        with patch('scripts.icon_audit.compare_icon', return_value=([{'name': 'Ref', 'score': 70}], False)), patch('scripts.icon_audit.enroll_icons', side_effect=IconEnrollmentConflict('index unchanged, compare again')):
            finding = self.audit()
        self.assertEqual(finding['status'], 'NOT_VERIFIABLE')
        self.assertEqual(finding['details'][0]['enrollment_status'], 'rejected')
        self.assertEqual(finding['details'][0]['matches'][0]['score'], 70)
        with patch('scripts.icon_api.build_opener') as build:
            build.return_value.open.side_effect = HTTPError('https://test.invalid', 409, 'Conflict', {}, io.BytesIO())
            with self.assertRaises(IconEnrollmentConflict):
                enroll_icons([bytes(PIXEL_BYTES)], 'App', 'a' * 64)

    def test_skip_reason_visible_when_highest_icon_is_boundary(self):
        self.icon('A', 'black')
        self.icon('B', 'white')
        configs = {self.root / 'App.xcconfig': 'ASSETCATALOG_COMPILER_APPICON_NAME = A\nASSETCATALOG_COMPILER_APPICON_NAME[sdk=iphoneos*] = B\n'}
        with patch('scripts.icon_audit.compare_icon', side_effect=[([{'name': 'Ref', 'score': 70}], False), ([{'name': 'Other', 'score': 85}], False)]), patch('scripts.icon_audit.enroll_icons') as enroll:
            finding = self.audit(False, configs)
        enroll.assert_not_called()
        self.assertEqual(finding['status'], 'PASS')
        self.assertIn('整体审计存在 FAIL 或 NOT_VERIFIABLE', finding['actual'])
        self.assertIn('85.0%', finding['actual'])

    def test_identity_ignores_external_vendor_and_nonmapping_plist(self):
        expected = hashlib.sha256(('path\0' + str(self.root.resolve())).encode()).hexdigest()
        inputs = {
            self.root / 'Pods' / 'Fake.xcconfig': 'PRODUCT_BUNDLE_IDENTIFIER = com.example.App\n',
            self.root / 'Info.plist': plistlib.dumps(['not-a-dictionary']).decode(),
            self.root / 'App.xcconfig': 'VALUE = "PRODUCT_BUNDLE_IDENTIFIER = com.fake.App"\n',
        }
        self.assertEqual(icon_project_key(self.root, inputs), expected)
        inputs = {self.root / 'CustomVendor' / 'App.xcconfig': 'PRODUCT_BUNDLE_IDENTIFIER = com.example.App\n'}
        self.assertEqual(icon_project_key(self.root, inputs, ('CustomVendor',)), expected)

    def test_batch_limit_prevents_partial_enrollment(self):
        images = [self.icon('App' + str(index), (index, 0, 0)).resolve() for index in range(17)]
        with patch('scripts.icon_audit.find_app_icons', return_value=(images, [])), patch('scripts.icon_audit.compare_icon', return_value=([{'name': 'Ref', 'score': 70}], False)), patch('scripts.icon_audit.enroll_icons') as enroll:
            finding = self.audit()
        enroll.assert_not_called()
        self.assertEqual(finding['status'], 'PASS')
        self.assertTrue(all(row['enrollment_status'] == 'skipped' for row in finding['details']))
        self.assertIn('超过单批 16', finding['actual'])

    def test_auditor_uses_all_other_normalized_checks_for_gate(self):
        self.icon('AppIcon', 'black')
        methods = ('check_code_lines', 'check_iap', 'check_ab_and_restore', 'check_permissions', 'check_legal_links', 'check_hidden_risks', 'check_ai_and_metadata', 'check_privacy_manifest', 'check_sensitive_terms', 'check_launch_and_name')
        for blocked in ('FAIL', 'NOT_VERIFIABLE', 'PASS', 'WARN'):
            with self.subTest(blocked=blocked):
                auditor = Auditor(self.root)
                for rule in RULE_ORDER:
                    if rule != 'IOS-003':
                        auditor.add(rule, blocked if rule == 'CODE-001' else 'PASS', 'info', 'test', actual='test')
                patches = [patch.object(auditor, method) for method in methods]
                for mock in patches: mock.start()
                try:
                    with patch('scripts.icon_audit.compare_icon', return_value=([{'name': 'Ref', 'score': 70}], False)), patch('scripts.icon_audit.enroll_icons', return_value=[True]) as enroll:
                        auditor.run()
                    self.assertEqual(enroll.called, blocked in ('PASS', 'WARN'))
                finally:
                    for mock in patches: mock.stop()

if __name__ == '__main__':
    unittest.main()
