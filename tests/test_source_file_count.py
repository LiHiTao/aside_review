import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.code_lines import audit_code_lines, audit_source_file_count
from scripts.audit_ios_a_side import Auditor, RULE_ORDER, markdown_report


class SourceFileCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, content='let value = 1\n'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def populate(self, count):
        for index in range(count):
            self.write(f'App/F{index}.swift')

    def test_strict_boundary(self):
        for count, expected in ((0, 'FAIL'), (19, 'FAIL'), (20, 'FAIL'), (21, 'PASS')):
            self.populate(count)
            result = audit_source_file_count(self.root, {})
            self.assertEqual(expected, result['status'])
            self.assertEqual(count, len(result['details']))

    def test_empty_comments_and_non_source_files_do_not_inflate(self):
        self.populate(20)
        for name, content in (('Empty.swift', ''), ('Blank.m', '\n '), ('Comments.hpp', '// hello\n/* comment */'),
                              ('Resource.html', 'text'), ('Config.json', '{}')):
            self.write(name, content)
        result = audit_source_file_count(self.root, {})
        self.assertEqual('FAIL', result['status'])
        self.assertEqual(20, len(result['details']))

    def test_source_scope_filters_and_overlapping_paths(self):
        for name in ('App/A.swift', 'App/B.m', 'App/C.mm', 'App/D.h', 'App/E.c',
                     'App/F.cc', 'App/G.cpp', 'App/H.hpp'):
            self.write(name)
        for name in ('Other/F.swift', 'App/Tests/F.swift', 'App/Pods/F.swift', 'App/Vendor/F.swift',
                     'App/Generated/F.swift', 'App/Ignore/F.swift', 'App/Excluded/F.swift'):
            self.write(name)
        (self.root / 'App/Alias.swift').symlink_to(self.root / 'App/A.swift')
        result = audit_source_file_count(self.root, {'source_file_threshold': 7,
            'a_side_source_paths': ['App', 'App/A.swift'], 'ignored_paths': ['Ignore'],
            'code_line_excluded_paths': ['App/Excluded']})
        self.assertEqual('PASS', result['status'])
        self.assertEqual(8, len(result['details']))

    def test_configuration_validation_and_zero_threshold(self):
        self.write('App.swift')
        for value in (True, False, -1, 20.0, '20', None):
            with self.subTest(value=value):
                self.assertEqual('NOT_VERIFIABLE', audit_source_file_count(self.root, {'source_file_threshold': value})['status'])
        self.assertEqual('PASS', audit_source_file_count(self.root, {'source_file_threshold': 0})['status'])
        for policy in ({'a_side_source_paths': ['missing']}, {'a_side_source_paths': '../outside'},
                       {'code_line_excluded_paths': ['*.swift']}):
            self.assertEqual('NOT_VERIFIABLE', audit_source_file_count(self.root, policy)['status'])

    def test_incomplete_scan_cannot_pass_a_partial_count(self):
        self.populate(21)
        bad = self.write('Bad.swift', '/* unclosed')
        self.assertEqual('NOT_VERIFIABLE', audit_source_file_count(self.root, {})['status'])
        bad.write_bytes(b'\xff')
        self.assertEqual('NOT_VERIFIABLE', audit_source_file_count(self.root, {})['status'])
        with patch.object(Path, 'read_text', side_effect=PermissionError):
            self.assertEqual('NOT_VERIFIABLE', audit_source_file_count(self.root, {})['status'])

    def test_incomplete_walk_and_external_symlink(self):
        def failed_walk(top, **kwargs):
            kwargs['onerror'](PermissionError())
            return iter(())
        with patch('scripts.code_lines.os.walk', side_effect=failed_walk):
            self.assertEqual('NOT_VERIFIABLE', audit_source_file_count(self.root, {})['status'])
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external) / 'External.swift'
            outside.write_text('let x = 1')
            (self.root / 'Link.swift').symlink_to(outside)
            self.assertEqual('NOT_VERIFIABLE', audit_source_file_count(self.root, {})['status'])

    def test_file_count_independent_of_loc_failure_and_invalid_loc_threshold(self):
        self.populate(21)
        for threshold in (5000, True, -1, '5000'):
            policy = {'code_line_threshold': threshold}
            lines = audit_code_lines(self.root, policy)
            self.assertNotEqual('PASS', lines['status'])
            self.assertEqual('PASS', audit_source_file_count(self.root, policy, lines)['status'])

    def test_top_level_report_schema_and_read_only(self):
        self.populate(21)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report = Auditor(self.root).run().report()
        self.assertEqual(19, len(RULE_ORDER))
        self.assertEqual(('CODE-001', 'CODE-002', 'CODE-003'), RULE_ORDER[:3])
        self.assertEqual(RULE_ORDER, tuple(item['id'] for item in report['findings']))
        self.assertEqual('PASS', next(item for item in report['findings'] if item['id'] == 'CODE-003')['status'])
        self.assertIn('CODE-003', markdown_report(report))
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
