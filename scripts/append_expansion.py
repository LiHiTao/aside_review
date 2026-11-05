"""Bounded Swift source check for repeated literal collection initialization."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re

try:
    from .code_lines import LexicalError, mask_source_comments
except ImportError:
    from code_lines import LexicalError, mask_source_comments

MIN_APPEND_CALLS = 100
_TOKEN = re.compile(
    r'(?P<space>\s+)|(?P<name>[^\W\d]\w*)'
    r'|(?P<number>0[xX][\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+'
    r'|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d[\d_]*)?)|(?P<symbol>.)'
)
_STRING_OPENING = re.compile(r'(#*)(?:"""|")')
_CLOSERS = {')': '(', ']': '[', '}': '{'}
_CONTINUED = {'return', 'throw', 'yield', 'await', 'try', 'if', 'while', 'guard', 'in', 'as', 'is'}


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int
    kind: str


def _tokens(clean, strings):
    tokens = []
    pos = 0
    while pos < len(clean):
        if pos in strings:
            end = strings[pos]
            tokens.append(_Token('<string>', pos, end, 'string'))
            pos = end
            continue
        match = _TOKEN.match(clean, pos)
        if match.lastgroup != 'space':
            tokens.append(_Token(match.group(), pos, match.end(), match.lastgroup))
        pos = match.end()
    return tokens


def _scopes(tokens, raw):
    pairs, scopes, stack = {}, [], []
    for index, token in enumerate(tokens):
        if token.text == '#' and raw.startswith('#/', token.start):
            raise LexicalError('重复追加检测无法解析 Swift 扩展分隔符正则字面量')
        scopes.append(stack[-1] if stack else None)
        if token.text in _CLOSERS.values():
            stack.append(index)
        elif token.text in _CLOSERS:
            if not stack or tokens[stack[-1]].text != _CLOSERS[token.text]:
                raise LexicalError('重复追加检测无法匹配 Swift 作用域或调用定界符')
            pairs[stack.pop()] = index
    if stack:
        raise LexicalError('重复追加检测发现未闭合的 Swift 作用域或调用')
    return pairs, scopes


def _static_string(clean, token):
    opening = _STRING_OPENING.match(clean, token.start)
    escape = re.escape('\\' + opening.group(1))
    # Pair each active escape with its next character. An escaped backslash
    # followed by an ordinary parenthesis does not start Swift interpolation.
    return not any(match.group(1) == '(' for match in
                   re.finditer(escape + r'([\s\S])', clean[opening.end():token.end]))


def _static_value(tokens, pairs, clean, start, limit, depth=0):
    """Return the next token for a recognized literal or constructor value."""
    if start >= limit or depth >= 24:
        return None
    token = tokens[start]
    if token.kind == 'string':
        return start + 1 if _static_string(clean, token) else None
    if token.kind == 'number' or token.text in {'true', 'false', 'nil'}:
        return start + 1
    if token.text in {'-', '+'}:
        return start + 2 if start + 1 < limit and tokens[start + 1].kind == 'number' else None

    if token.text == '[':
        opening = start
    else:
        # Accept upper-case constructor paths and Swift shorthand .init(...)
        # or enum cases, but leave ordinary variables and function calls out.
        shorthand = token.text == '.'
        if shorthand:
            start += 1
        if start >= limit or tokens[start].kind != 'name':
            return None
        if not shorthand and not tokens[start].text.lstrip('_')[:1].isupper():
            return None
        opening = start + 1
        while opening + 1 < limit and tokens[opening].text == '.' and tokens[opening + 1].kind == 'name':
            opening += 2
        if shorthand and (opening >= limit or tokens[opening].text != '('):
            return opening
        if opening >= limit or tokens[opening].text != '(':
            return None
        name = tokens[opening - 1].text
        if name != 'init' and (shorthand or not name.lstrip('_')[:1].isupper()):
            return None

    closing = pairs.get(opening, limit)
    if closing >= limit:
        return None
    index = opening + 1
    if tokens[opening].text == '(' and index == closing:
        return None
    while index < closing:
        if tokens[index].kind == 'name' and index + 1 < closing and tokens[index + 1].text == ':':
            index += 2
        next_index = _static_value(tokens, pairs, clean, index, closing, depth + 1)
        if next_index is None:
            return None
        if next_index < closing and tokens[next_index].text != ',':
            return None
        index = next_index + 1
    return closing + 1


def _standalone(tokens, clean, start, end):
    before = tokens[start - 1]
    if before.text not in {'{', ';'} and (
        '\n' not in clean[before.end:tokens[start].start] or before.text in _CONTINUED
        or (before.kind == 'symbol' and before.text not in {')', ']', '}', '!', '?'})
    ):
        return False
    after = tokens[end + 1]
    if after.text in {'}', ';'}:
        return True
    return '\n' in clean[tokens[end].end:after.start] and (
        (after.kind == 'name' and after.text not in {'as', 'is'}) or after.text in {'#', '@'}
    )


def find_append_expansions(raw: str) -> list[dict]:
    """Identify runs of standalone literal append sites and count covered lines.

    Scope and payload checks are lexical. The result does not infer receiver
    types, runtime repetition, or which build configurations compile a run.
    An unparseable candidate file raises LexicalError.
    """
    if raw.count('append') < MIN_APPEND_CALLS:
        return []
    raw = raw.replace('\r\n', '\n').replace('\r', '\n')
    strings: dict[int, int] = {}
    try:
        clean = mask_source_comments(raw, '.swift', swift_strings=strings)
    except RecursionError as error:
        raise LexicalError('重复追加检测发现超出解析深度的 Swift 字符串') from error
    tokens = _tokens(clean, strings)
    pairs, scopes = _scopes(tokens, raw)
    line_starts = [0, *(m.end() for m in re.finditer('\n', clean))]
    source_lines = clean.split('\n')
    effective = [bool(line.strip()) for line in source_lines]
    results, run = [], []

    def record_run():
        if len(run) < MIN_APPEND_CALLS:
            return
        # Drop calls on boundary lines shared with unrelated statements.
        first, last = 0, len(run)
        while last - first >= MIN_APPEND_CALLS:
            start, _, first_line, _ = run[first]
            if not clean[line_starts[first_line - 1]:tokens[start].start].strip():
                break
            while first < last and run[first][2] == first_line:
                first += 1
        while last - first >= MIN_APPEND_CALLS:
            _, end, _, last_line = run[last - 1]
            line_end = line_starts[last_line] if last_line < len(line_starts) else len(clean)
            if clean[tokens[end].end:line_end].strip() in {'', ';'}:
                break
            while last > first and run[last - 1][3] == last_line:
                last -= 1
        if last - first < MIN_APPEND_CALLS:
            return
        selected = run[first:last]
        start, _, first_line, _ = selected[0]
        _, _, _, last_line = selected[-1]
        # A physical line shared by calls counts once.
        lines, counted_to = 0, 0
        for _, _, line, end_line in selected:
            lines += sum(effective[max(line, counted_to + 1) - 1:end_line])
            counted_to = end_line
        results.append({'receiver': tokens[start].text, 'line': first_line, 'end_line': last_line,
                        'append_count': len(selected), 'effective_lines': lines})

    for index in range(1, len(tokens) - 3):
        receiver = tokens[index]
        scope = scopes[index]
        if receiver.kind != 'name' or scope is None or tokens[scope].text != '{':
            continue
        if tuple(token.text for token in tokens[index + 1:index + 4]) != ('.', 'append', '('):
            continue
        end = pairs[index + 3]
        if end + 1 >= len(tokens) or not _standalone(tokens, clean, index, end):
            continue
        if _static_value(tokens, pairs, clean, index + 4, end) != end:
            continue
        if run:
            previous_start, previous_end, _, _ = run[-1]
            contiguous = index == previous_end + 1 or (
                index == previous_end + 2 and tokens[index - 1].text == ';'
            )
            if not contiguous or scopes[previous_start] != scope or tokens[previous_start].text != receiver.text:
                record_run()
                run = []
        run.append((index, end, bisect_right(line_starts, receiver.start),
                    bisect_right(line_starts, tokens[end].start)))
    record_run()
    return results
