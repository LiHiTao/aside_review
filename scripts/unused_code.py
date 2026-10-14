"""Conservative source-only unused private Swift function candidates.

This deliberately does not prove runtime unreachability or cover all dead code.
"""
from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import re
from typing import Mapping

try:
    from .code_lines import audit_code_lines, count_source_lines, LexicalError
    from .restore_detection import _lex
except ImportError:
    from code_lines import audit_code_lines, count_source_lines, LexicalError
    from restore_detection import _lex

FUNCTION = re.compile(r'\bfunc\s+([A-Za-z_]\w*)\s*(?:<[^{};]*?>\s*)?\(')
TYPE = re.compile(r'\b(class|struct|enum|actor|protocol|extension)\s+([A-Za-z_]\w*)[^{};]*\{')
NOTICE = '仅统计静态未发现外部引用的 Swift private/fileprivate 函数候选，不证明运行时无用；不覆盖全部无用代码。'
DEFAULTS = {'unused_code_warn_ratio': .10, 'unused_code_warn_lines': 300,
            'unused_code_high_ratio': .20, 'unused_code_high_lines': 1000}


def audit_unused_code(root: Path, policy: Mapping, sources: Mapping[Path, str] | None = None,
                      all_texts: Mapping[Path, str] | None = None, *,
                      code_result: dict | None = None, scan_incomplete: bool = False) -> dict:
    root = root.resolve()
    finding = {'id': 'CODE-002', 'status': 'NOT_VERIFIABLE', 'severity': 'medium',
               'title': '疑似无用代码', 'expected': '疑似无用代码少于建议复核门槛（项目经验标准）',
               'actual': '', 'evidence': [], 'manual_check': None, 'details': []}
    thresholds = {key: policy.get(key, value) for key, value in DEFAULTS.items()}
    invalid = []
    for key, value in thresholds.items():
        if key.endswith('_ratio'):
            valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
        else:
            valid = not isinstance(value, bool) and isinstance(value, int) and value >= 0
        if not valid:
            invalid.append(key)
    if not invalid and (thresholds['unused_code_high_ratio'] < thresholds['unused_code_warn_ratio'] or
                        thresholds['unused_code_high_lines'] < thresholds['unused_code_warn_lines']):
        invalid.append('high 门槛不得低于 warn 门槛')
    if invalid:
        finding['actual'] = '策略配置无效：' + '、'.join(invalid)
        finding['manual_check'] = '比例需为 0 至 1 的有限数字，行数需为非负整数；修正后重新扫描。'
        return finding
    counted = code_result if code_result is not None else audit_code_lines(root, policy)
    selected = [d for d in counted.get('details', []) if d.get('status') == 'PASS' and d.get('path')]
    denominator = sum(d.get('code_lines', 0) for d in selected)
    if counted.get('status') == 'NOT_VERIFIABLE' or scan_incomplete or not denominator:
        finding['actual'] = '源码或 A 面统计不完整，或有效代码行数为零，无法可靠计算疑似无用代码占比。'
        finding['manual_check'] = '确认源码读取完整及 A 面统计范围后重新扫描。'
        return finding
    source_map = {p.resolve(): raw for p, raw in (sources or {}).items()}
    selected_swift = [root / d['path'] for d in selected if Path(d['path']).suffix.lower() == '.swift']
    if not selected_swift:
        finding['actual'] = '当前统计范围没有支持的 Swift 源码；C/Objective-C 无用方法不作推断。'
        return finding
    try:
        for path in selected_swift:
            if path.resolve() not in source_map:
                source_map[path.resolve()] = path.read_text(encoding='utf-8-sig')
        lexed = {}
        for path, raw in source_map.items():
            normalized = raw.replace('\r\n', '\n').replace('\r', '\n')
            count_source_lines(normalized, path.suffix)
            lexed[path] = _lex(normalized)
    except (OSError, UnicodeError, LexicalError, RecursionError):
        finding['actual'] = '源码读取或词法解析不完整，无法可靠识别外部引用。'
        return finding
    refs = {p: triple[0] for p, triple in lexed.items()}
    if all_texts is None:
        # Standalone callers need the same resource-reference protection as Auditor.
        try:
            all_texts = {path: path.read_text(encoding='utf-8-sig') for path in root.rglob('*')
                         if path.is_file() and not path.is_symlink() and path.suffix.lower() in
                         {'.storyboard', '.xib', '.plist', '.strings', '.xcstrings'}}
        except (OSError, UnicodeError):
            finding['actual'] = '资源中的动态引用无法完整读取，无法可靠识别疑似无用代码。'
            return finding
    for path, raw in all_texts.items():
        if path.suffix.lower() in {'.storyboard', '.xib', '.plist', '.strings', '.xcstrings'}:
            refs[path.resolve()] = re.sub(r'<!--.*?-->', '', raw, flags=re.S)
    names = Counter(m.group(1) for triple in lexed.values() for m in FUNCTION.finditer(triple[1]))
    candidates, line_union, skipped_files = [], {}, []
    for path in selected_swift:
        path = path.resolve()
        clean, code, _ = lexed[path]
        # Conditional/macro and dynamically synthesized names defeat source-only proof.
        if re.search(r'^\s*#(?:if|elseif|else|endif)\b|\b(?:macro|NSSelectorFromString|NSClassFromString|sel_registerName|class_getMethodImplementation)\b|#/|/[^\n]*/', code, re.M):
            skipped_files.append(str(path.relative_to(root)))
            continue
        stack, ends = [], {}
        malformed = False
        for pos, char in enumerate(code):
            if char == '{':
                stack.append(pos)
            elif char == '}':
                if not stack:
                    malformed = True
                    break
                ends[stack.pop()] = pos
        if stack or malformed:
            finding['actual'] = 'Swift 作用域无法完整解析，无法可靠计算疑似无用代码。'
            return finding
        types = []
        for match in TYPE.finditer(code):
            opening = match.end() - 1
            prefix = code[max(code.rfind('}', 0, match.start()), code.rfind('{', 0, match.start()), code.rfind(';', 0, match.start())) + 1:match.start()]
            unsafe = match.group(1) in {'protocol', 'extension'} or ':' in match.group() or '@' in prefix
            types.append((opening, ends.get(opening, len(code)), unsafe))
        for match in FUNCTION.finditer(code):
            name = match.group(1)
            delimiter = max(code.rfind('}', 0, match.start()), code.rfind('{', 0, match.start()), code.rfind(';', 0, match.start()))
            prefix = code[delimiter + 1:match.start()]
            access = re.search(r'\b(?:private|fileprivate)\s+(?:(?:static|class|final|nonisolated|mutating|nonmutating)\s+)*$', prefix)
            if not access or '@' in prefix or re.search(r'\b(?:override|dynamic|required)\b', prefix) or names[name] != 1:
                continue
            if any(start < match.start() < end and unsafe for start, end, unsafe in types):
                continue
            start = delimiter + 1 + access.start()
            # Do not count another declaration's physical line as suspected code.
            firstline = code.rfind('\n', 0, start) + 1
            if code[firstline:start].strip():
                continue
            pos, depth = match.end(), 1
            while pos < len(code) and depth:
                depth += (code[pos] == '(') - (code[pos] == ')')
                pos += 1
            if depth:
                continue
            opening = code.find('{', pos)
            if opening < 0 or re.search(r'[;{}]|\b(?:func|class|struct|enum)\b', code[pos:opening]):
                continue
            end = ends.get(opening)
            if end is None:
                continue
            pattern = re.compile(r'\b' + re.escape(name) + r'\b')
            used = any(pattern.search(text if other != path else text[:start] + ' ' * (end + 1 - start) + text[end + 1:])
                       for other, text in refs.items())
            if used:
                continue
            line = clean.count('\n', 0, start) + 1
            endline = clean.count('\n', 0, end) + 1
            lines = clean.splitlines()
            effective = {n for n in range(line, endline + 1) if lines[n - 1].strip()}
            line_union.setdefault(path, set()).update(effective)
            candidates.append({'label': name, 'name': name, 'path': str(path.relative_to(root)),
                               'line': line, 'end_line': endline, 'effective_lines': len(effective),
                               'status': 'WARN', 'actual': '未发现函数声明及函数体之外的同名引用'})
    if len(skipped_files) == len(selected_swift):
        finding['actual'] = '统计范围内 Swift 文件均含条件编译、宏或动态派发等不支持结构，未获得可分析的覆盖范围。'
        finding['details'] = [{'label': '覆盖范围', 'status': 'NOT_VERIFIABLE', 'actual': NOTICE, 'skipped_files': skipped_files}]
        return finding
    suspected = sum(len(lines) for lines in line_union.values())
    ratio = suspected / denominator
    high = suspected > 0 and suspected >= thresholds['unused_code_high_lines'] and ratio >= thresholds['unused_code_high_ratio']
    warn = suspected > 0 and suspected >= thresholds['unused_code_warn_lines'] and ratio >= thresholds['unused_code_warn_ratio']
    finding['status'] = 'WARN' if warn or high else 'PASS'
    finding['severity'] = 'high' if high else 'medium' if warn else 'info'
    level = '高占比复核' if high else '建议复核' if warn else '覆盖范围内未超过建议门槛'
    finding['actual'] = f'疑似无用代码 {suspected:,}/{denominator:,} 行（{ratio:.1%}），{len(candidates)} 个函数候选；{len(selected_swift) - len(skipped_files)} 个 Swift 文件参与分析，{len(skipped_files)} 个文件跳过；{level}。' + NOTICE
    finding['details'] = [{'label': '统计与覆盖范围', 'status': finding['status'], 'actual': NOTICE,
                           'suspected_lines': suspected, 'total_code_lines': denominator, 'suspected_ratio': ratio,
                           'candidate_count': len(candidates), 'supported_coverage': 'Swift private/fileprivate 显式函数体；不覆盖类型、C/Objective-C、运行时派发、协议实现、宏或条件编译文件',
                           'swift_files': len(selected_swift), 'skipped_files': skipped_files, 'thresholds': thresholds}] + candidates
    finding['evidence'] = [{'path': c['path'], 'line': c['line'], 'excerpt': f"{c['name']}：第 {c['line']}–{c['end_line']} 行，有效 {c['effective_lines']} 行，静态未发现外部引用"}
                           for c in sorted(candidates, key=lambda c: (-c['effective_lines'], c['path'], c['line']))[:10]]
    if finding['status'] == 'WARN':
        finding['manual_check'] = '逐项确认候选是否由反射、框架或动态机制调用；确认后再清理。本结果不自动删除代码。'
    return finding
