import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from code_lines import audit_code_lines, count_source_lines, LexicalError


class CodeLineLexerTests(unittest.TestCase):
    def test_blank_comment_mixed_and_braces(self):
        result = count_source_lines('// comment\n\n/* block\n * body\n */\nimport Swift // trailing\n{\n}\n')
        self.assertEqual(dict(total_lines=8, blank_lines=1, comment_lines=4, code_lines=3), result)

    def test_nested_swift_comments(self):
        self.assertEqual(2, count_source_lines('let x = 1 /* outer\n /* inner */\n*/\nlet y = 2')['code_lines'])

    def test_swift_strings_raw_multiline_and_interpolation(self):
        source = '''let url = "https://a.test/*b*/"
let quote = "\\\" // still string"
let raw = ##"""
// literal
/* literal */

"""##
let value = "\\(foo("nested // text")) end"
'''
        self.assertEqual(dict(total_lines=8, blank_lines=1, comment_lines=0, code_lines=7), count_source_lines(source))

    def test_interpolation_comments_not_literal_text(self):
        source = 'let x = """\n\\(foo(\n// real comment\n"bar // string"))\n"""'
        self.assertEqual(4, count_source_lines(source)['code_lines'])

    def test_crlf_cr_and_no_final_newline(self):
        self.assertEqual(dict(total_lines=4, blank_lines=1, comment_lines=1, code_lines=2), count_source_lines('x\r\n// c\r\n\r\ny'))
        self.assertEqual(2, count_source_lines('x\ry')['total_lines'])
        self.assertEqual(0, count_source_lines('')['total_lines'])
        self.assertEqual(1, count_source_lines('\n')['blank_lines'])

    def test_c_characters_raw_and_digit_separators(self):
        source = '''char c = '\\'';
auto n = 1'000;
auto text = R"tag(
// literal " text
)tag";
// comment
'''
        self.assertEqual(5, count_source_lines(source, '.cpp')['code_lines'])

    def test_c_line_comment_continuation(self):
        self.assertEqual(1, count_source_lines('// a \\\nnot code\nx', '.c')['code_lines'])

    def test_unclosed_lexical_constructs(self):
        for text, suffix in [('/* x', '.swift'), ('"abc', '.swift'), ('"abc\nx', '.swift'), ('#"abc', '.swift'), ('"\\(abc', '.swift'), ('R"tag(x', '.cpp'), ('\x00', '.swift')]:
            with self.subTest(text=text), self.assertRaises(LexicalError):
                count_source_lines(text, suffix)


class CodeLineAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def audit(self, **policy):
        return audit_code_lines(self.root, {'a_side_source_paths': ['.'], **policy})

    def test_strict_boundary(self):
        for lines, status in [(4999, 'FAIL'), (5000, 'FAIL'), (5001, 'PASS')]:
            self.write('App.swift', 'let x = 0\n' * lines)
            self.assertEqual(status, self.audit()['status'])

    def test_default_scopes_use_project_root_and_strict_boundary(self):
        for policy in ({}, {'a_side_source_paths': []}):
            for lines, status in ((5000, 'FAIL'), (5001, 'PASS')):
                with self.subTest(policy=policy, lines=lines):
                    self.write('App.swift', 'x\n' * lines)
                    result = audit_code_lines(self.root, policy)
                    self.assertEqual(status, result['status'])
                    self.assertEqual(lines, result['details'][0]['code_lines'])

    def test_default_empty_project_is_zero_fail(self):
        for policy in ({}, {'a_side_source_paths': []}):
            with self.subTest(policy=policy):
                result = audit_code_lines(self.root, policy)
                self.assertEqual('FAIL', result['status'])
                self.assertIn('0 行，0 个文件', result['actual'])

    def test_default_scope_preserves_source_filters_and_exclusions(self):
        self.write('App/Main.swift', 'x\n' * 5000)
        for name in ['Pods/A.swift', 'FeatureTests/A.swift', 'B_side/A.swift',
                     'Generated/A.swift', 'FooTest.swift', 'api.generated.swift',
                     'Ignore/A.swift', 'Custom/A.swift', 'UI.storyboard',
                     'project.pbxproj', 'terms.html']:
            self.write(name, 'x\n' * 5001)
        for scope_policy in ({}, {'a_side_source_paths': []}):
            with self.subTest(policy=scope_policy):
                result = audit_code_lines(self.root, {
                    **scope_policy, 'ignored_paths': ['Ignore'],
                    'code_line_excluded_paths': ['Custom']})
                self.assertEqual('FAIL', result['status'])
                self.assertEqual(['App/Main.swift'], [item['path'] for item in result['details']])
                self.assertEqual(5000, result['details'][0]['code_lines'])

    def test_explicit_empty_directory_is_zero_fail(self):
        result = self.audit()
        self.assertEqual('FAIL', result['status'])
        self.assertIn('0 行，0 个文件', result['actual'])

    def test_extension_scope_dedup_and_exclusions(self):
        for name in ['App/Main.swift', 'App/Support.hpp', 'App/Latest.swift']:
            self.write(name, 'x')
        for name in ['App/Pods/A.swift', 'App/FeatureTests/A.swift', 'App/B_side/A.swift', 'App/Generated/A.swift', 'App/FooTest.swift', 'App/test_foo.c', 'App/api.generated.swift', 'App/Ignore/A.swift', 'App/Custom/A.swift', 'App/UI.storyboard', 'App/project.pbxproj', 'App/terms.html']:
            self.write(name, 'x\n' * 9000)
        result = self.audit(a_side_source_paths=['App', 'App/Main.swift'], code_line_threshold=2,
                            ignored_paths=['Ignore'], code_line_excluded_paths=['App/Custom'])
        self.assertEqual('PASS', result['status'])
        self.assertEqual(3, sum(item['code_lines'] for item in result['details']))
        self.assertEqual(3, len(result['details']))
        self.assertTrue(all(not Path(item['path']).is_absolute() for item in result['evidence']))

    def test_explicit_scope_excludes_other_directories(self):
        self.write('A/Main.swift', 'x')
        self.write('Other/Main.swift', 'x\n' * 9999)
        self.assertEqual('FAIL', self.audit(a_side_source_paths=['A'])['status'])

    def test_invalid_configs_and_paths(self):
        self.write('Good.swift', 'x\n' * 5001)
        for policy in [{'a_side_source_paths': None}, {'a_side_source_paths': ['']},
                       {'a_side_source_paths': [2]}, {'a_side_source_paths': 'A'}, {'a_side_source_paths': ['missing']},
                       {'a_side_source_paths': ['../outside']}, {'a_side_source_paths': [str(self.root)]},
                       {'code_line_threshold': True}, {'code_line_threshold': -1},
                       {'code_line_excluded_paths': [2]}, {'code_line_excluded_paths': ['*.swift']}, {'code_line_excluded_paths': ['../outside']}, {'code_line_excluded_paths': ['/outside']}, {'ignored_paths': None}]:
            with self.subTest(policy=policy):
                self.assertEqual('NOT_VERIFIABLE', self.audit(**policy)['status'])
        self.write('data.json', '{}')
        self.assertEqual('NOT_VERIFIABLE', self.audit(a_side_source_paths=['data.json'])['status'])

    def test_read_decode_and_lexical_errors_never_pass_partial_total(self):
        self.write('Good.swift', 'x\n' * 5001)
        bad = self.write('Bad.swift', '/* unclosed')
        self.assertEqual('NOT_VERIFIABLE', self.audit()['status'])
        bad.write_bytes(b'\xff')
        self.assertEqual('NOT_VERIFIABLE', self.audit()['status'])
        with patch.object(Path, 'read_text', side_effect=PermissionError):
            self.assertEqual('NOT_VERIFIABLE', self.audit()['status'])

    def test_symlink_scope_and_file_bounds(self):
        self.write('App/Main.swift', 'x')
        (self.root / 'Alias').symlink_to(self.root / 'App', target_is_directory=True)
        self.assertEqual(1, len(self.audit()['details']))
        self.assertEqual('NOT_VERIFIABLE', self.audit(a_side_source_paths=['Alias'])['status'])
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'Outside.swift'
            target.write_text('x', encoding='utf-8')
            (self.root / 'Outside.swift').symlink_to(target)
            self.assertEqual('NOT_VERIFIABLE', self.audit()['status'])

    def test_file_symlink_dedup(self):
        self.write('A.swift', 'x')
        (self.root / 'B.swift').symlink_to(self.root / 'A.swift')
        self.assertEqual(1, len(self.audit()['details']))

    def test_exclusions_are_exact_root_relative_and_alias_safe(self):
        self.write('Custom/A.swift', 'x')
        self.write('App/Custom/A.swift', 'x')
        self.write('Pods/Third.swift', 'x')
        (self.root / 'Alias.swift').symlink_to(self.root / 'Pods/Third.swift')
        result = self.audit(code_line_excluded_paths=['./Custom/'])
        self.assertEqual(['App/Custom/A.swift'], [item['path'] for item in result['details']])

    def test_walk_failure(self):
        def failing_walk(top, **kwargs):
            kwargs['onerror'](PermissionError())
            return iter(())
        with patch('code_lines.os.walk', side_effect=failing_walk):
            self.assertEqual('NOT_VERIFIABLE', self.audit()['status'])


if __name__ == '__main__':
    unittest.main()
