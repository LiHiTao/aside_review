import unittest

from scripts.append_expansion import find_append_expansions
from scripts.code_lines import LexicalError


def repeated(count, receiver='items', expression='Row(value: 1, title: "sample")'):
    return ''.join(f'    {receiver}.append({expression})\n' for _ in range(count))


class AppendExpansionTests(unittest.TestCase):
    def test_pupa_scale_two_distinct_functions(self):
        raw = (
            'func captions() {\n    var items: [Row] = []\n'
            + repeated(2500)
            + '    return items\n}\n'
            + 'func palettes() {\n    var list: [Swatch] = []\n'
            + repeated(800, 'list', 'Swatch(name: "Berry", hex: "C2175C")')
            + '    return list\n}\n'
        )
        self.assertEqual(find_append_expansions(raw), [
            dict(receiver='items', line=3, end_line=2502, append_count=2500, effective_lines=2500),
            dict(receiver='list', line=2507, end_line=3306, append_count=800, effective_lines=800),
        ])

    def test_inclusive_threshold_and_compact_loop(self):
        self.assertEqual(find_append_expansions('func add() {\n' + repeated(99) + '}'), [])
        raw = 'func add() {\n' + repeated(100, expression='42') + '}'
        self.assertEqual(find_append_expansions(raw)[0]['append_count'], 100)
        self.assertEqual(find_append_expansions('for i in 0..<2500 {\n items.append(i)\n}'), [])

    def test_multiline_raw_string_and_comments(self):
        item = (
            '    items\n'
            '      .append(\n'
            '        Row(\n'
            '          id: -0x10,\n'
            '          /* comment */\n'
            '\n'
            '          title: #"example.append() // literal"#,\n'
            '          layout: .compact\n'
            '        )\n'
            '      ) // append note\n'
        )
        raw = ('func add() {\n' + item * 100 + '}\n').replace('\n', '\r\n')
        self.assertEqual(find_append_expansions(raw), [
            dict(receiver='items', line=2, end_line=1001, append_count=100, effective_lines=800),
        ])

    def test_same_line_calls_count_once_without_claiming_other_code(self):
        pairs = '    items.append(1); items.append(2)\n' * 50
        raw = 'func add() {\n' + pairs + '}'
        self.assertEqual(find_append_expansions(raw), [
            dict(receiver='items', line=2, end_line=51, append_count=100, effective_lines=50),
        ])
        raw = 'func add() {\n' + pairs[:-1] + '; use(items)\n}'
        self.assertEqual(find_append_expansions(raw), [])
        raw = 'func add() { var items: [Int] = []; items.append(0)\n' + pairs + '    items.append(3); use(items)\n}'
        self.assertEqual(find_append_expansions(raw), [
            dict(receiver='items', line=2, end_line=51, append_count=100, effective_lines=50),
        ])

    def test_unrelated_scopes_receivers_and_statements_split_blocks(self):
        examples = [
            repeated(60) + repeated(60, 'other'),
            repeated(60) + '    use(items)\n' + repeated(60),
            repeated(60) + '    if useOther {\n' + repeated(60) + '    }\n',
            repeated(60) + '#if DEBUG\n' + repeated(60) + '#endif\n',
        ]
        for example in examples:
            with self.subTest(example=example[:120]):
                self.assertEqual(find_append_expansions('func add() {\n' + example + '}'), [])

    def test_ignores_comments_literals_runtime_values_and_other_expression_contexts(self):
        calls = repeated(100)
        examples = [
            '/*\n' + calls + '*/\n',
            'let example = """\n' + calls.replace('"sample"', '1') + '"""\n',
            'let example = """\n\\(render("""\n' + calls.replace('"sample"', '1') + '"""))\n"""\n',
            repeated(100, expression='Row(value: input)'),
            repeated(100, expression='Row(value: await make())'),
            repeated(100, expression='Factory.make(value: 1)'),
            repeated(100, expression='.make(value: 1)'),
            repeated(100, expression='Row(value: "prefix \\(value)")'),
            '    _ = items.append(1)\n' * 100,
            '    model.items.append(1)\n' * 100,
            '    use(\n        items.append(1)\n    )\n' * 100,
        ]
        for example in examples:
            with self.subTest(example=example[:120]):
                self.assertEqual(find_append_expansions('func add() {\n' + example + '}'), [])

    def test_malformed_source_is_unverifiable(self):
        for tail in ('', '}\n/*', '}\nlet broken = "'):
            raw = 'func add() {\n' + repeated(100) + tail
            with self.subTest(tail=tail), self.assertRaises(LexicalError):
                find_append_expansions(raw)

    def test_extended_regex_is_unverifiable(self):
        for opener in ('#/', '#//', '##/'):
            raw = f'let example = {opener}\nfunc add() {{\n' + repeated(100, expression='1') + '}\n/#'
            with self.subTest(opener=opener), self.assertRaises(LexicalError):
                find_append_expansions(raw)
