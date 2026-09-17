"""Small lexical StoreKit version check; does not prove transaction correctness."""
from __future__ import annotations

from pathlib import Path
import re

try:
    from .restore_detection import _lex
except ImportError:
    from restore_detection import _lex

_BOUNDARY = r'(?<![\w.])'
_NOTICE = '仅确认存在 StoreKit 2 API 使用，不验证购买成功、发货或完整交易流程。'


def analyze_storekit_usage(root: Path, sources: dict[Path, str], *, scan_incomplete: bool = False) -> dict:
    modern, legacy = [], []
    # Project-local type declarations override imported names, including in other files.
    codes = {p: _lex(raw)[1] for p, raw in sources.items() if p.suffix == '.swift'}
    types = set()
    for code in codes.values():
        for declaration in re.finditer(r'\b(?:class|struct|enum|actor|protocol|typealias)\s+(\w+)', code):
            before = code[:declaration.start()]
            if before.count('{') == before.count('}'):
                types.add(declaration.group(1))

    for path, raw in sources.items():
        if path.suffix not in {'.swift', '.m', '.mm'}:
            continue
        clean, code, _ = _lex(raw)
        imported = bool(re.search(r'\bimport\s+(?:(?:class|struct|enum|protocol)\s+)?StoreKit\b', code))
        scopes, stack, ends = {}, [], {}
        for pos, char in enumerate(code):
            scopes[pos] = tuple(stack)
            if char == '{':
                stack.append(pos)
            elif char == '}' and stack:
                ends[stack.pop()] = pos
        function_regions = []
        for function in re.finditer(r'\b(?:func\s+\w+(?:\s*<[^>{}]*>)?|init[?!]?)\s*\(', code):
            opening = code.find('(', function.start())
            depth, closing = 1, opening + 1
            while closing < len(code) and depth:
                depth += (code[closing] == '(') - (code[closing] == ')')
                closing += 1
            body = code.find('{', closing)
            if body >= 0 and not re.search(r'[;{}]|\b(?:func|class|struct)\b', code[closing:body]):
                function_regions.append((function.start(), opening, closing, body, ends.get(body, len(code))))

        def binding_scope(decl):
            for start, opening, closing, body, end in function_regions:
                if opening < decl.start() < closing:
                    return scopes.get(body, ()) + (body,)
            return scopes.get(decl.start(), ())

        declarations = list(re.finditer(r'\b(?:let|var|func)\s+(\w+)\b|(?<![\w.])(\w+)\s*:\s*(?!:)', code))

        def visible(decl, pos):
            scope = binding_scope(decl)
            current = scopes.get(pos, ())
            return current[:len(scope)] == scope

        def closure_bindings(body, pos):
            header = re.match(r'\s*(?:\([^{}]*?\)|[^{};]*?)\s+in\b', code[body+1:pos])
            if not header:
                return {}
            content = re.sub(r'\s+in\s*$', '', header.group()).strip().strip('()')
            result = {}
            for part in content.split(','):
                binding = re.fullmatch(r'\s*(\w+)\s*(?::\s*((?:StoreKit\s*\.\s*)?Product)\s*)?', part)
                if binding:
                    result[binding.group(1)] = binding.group(2)
                else:
                    binding = re.match(r'\s*(\w+)\s*:', part)
                    if binding:
                        result[binding.group(1)] = None
            return result

        def shadowed(name, pos):
            if name in types:
                return True
            for declaration in re.finditer(r'\b(?:class|struct|enum|actor|protocol|typealias)\s+' + re.escape(name) + r'\b', code):
                if visible(declaration, pos):
                    return True
            for start, opening, closing, body, end in function_regions:
                if start < pos < end and re.search(r'<[^>]*\b' + re.escape(name) + r'\b', code[start:opening]):
                    return True
            for declaration in re.finditer(r'\b(?:class|struct|enum|actor)\s+\w+\s*<([^>{}]+)>[^{}]*\{', code):
                body = declaration.end() - 1
                if declaration.start() < pos < ends.get(body, len(code)) and re.search(r'\b' + re.escape(name) + r'\b', declaration.group(1)):
                    return True
            for body in scopes.get(pos, ()):
                if name in closure_bindings(body, pos):
                    return True
            return any((d.group(1) or d.group(2)) == name and visible(d, pos) for d in declarations)

        def accepted(qualified, name, pos):
            if code[:pos].rstrip().endswith('.'):
                return False
            if qualified:
                return not shadowed('StoreKit', pos)
            return imported and not shadowed(name, pos)

        def evidence(match, bucket):
            pos = match.start()
            line = raw.count('\n', 0, pos) + 1
            excerpt = raw[pos:match.end()].strip()
            bucket.append({'path': str(path.relative_to(root)), 'line': line, 'excerpt': excerpt})

        if path.suffix == '.swift':
            patterns = (
                ('Product', r'products\s*\(\s*for\s*:'),
                ('Transaction', r'(?:updates|currentEntitlements|unfinished|all)\b(?!\s*\()|latest\s*\(\s*for\s*:'),
            )
            for name, member in patterns:
                pattern = _BOUNDARY + r'(?P<module>StoreKit\s*\.\s*)?' + name + r'\s*\.\s*(?:' + member + ')'
                for match in re.finditer(pattern, code):
                    if match.group().rstrip().endswith(':') and clean[match.end():].lstrip().startswith(')'):
                        continue
                    if accepted(match.group('module'), name, match.start()):
                        evidence(match, modern)
            for match in re.finditer(_BOUNDARY + r'(?P<module>StoreKit\s*\.\s*)?(?P<name>ProductView|StoreView|SubscriptionStoreView)\s*\(', code):
                if accepted(match.group('module'), match.group('name'), match.start()):
                    evidence(match, modern)
            # Explicitly typed variables and parameters only. Do not infer ordinary purchase().
            typed = list(re.finditer(r'\b(?P<name>\w+)\s*:\s*(?P<module>StoreKit\s*\.\s*)?Product\b(?!\s*\.)\s*[!?]?', code))
            for match in re.finditer(_BOUNDARY + r'(?P<self>self\s*\.\s*)?(?P<name>\w+)\s*[!?]?\s*\.\s*purchase\s*\(', code):
                name, pos = match.group('name'), match.start()
                if code[:pos].rstrip().endswith('.'):
                    continue
                candidates = [d for d in declarations if (d.group(1) or d.group(2)) == name and visible(d, pos)]
                if not candidates:
                    continue
                if match.group('self'):
                    candidates = [d for d in candidates if not any(o < d.start() < c or b < d.start() < e for _, o, c, b, e in function_regions)]
                # Untyped closure parameters and for-loop bindings are not Product evidence.
                closure_shadow = False
                for body in scopes.get(pos, ()):
                    bindings = closure_bindings(body, pos)
                    if name in bindings and not bindings[name]:
                        closure_shadow = True
                if closure_shadow:
                    continue
                if re.search(r'\bfor\s+' + re.escape(name) + r'\s+in\b', code[:pos]):
                    continue
                if not candidates:
                    continue
                nearest = max(candidates, key=lambda d: (len(binding_scope(d)), d.start()))
                for decl in typed:
                    # let/var declaration begins before the identifier; require same binding.
                    if nearest.start() <= decl.start() <= nearest.end() and accepted(decl.group('module'), 'Product', decl.start()):
                        evidence(match, modern)
                        break
            for match in re.finditer(_BOUNDARY + r'(?P<module>StoreKit\s*\.\s*)?SKPaymentQueue\s*\.\s*default\s*\(\s*\)\s*\.\s*add\s*\(', code):
                if accepted(match.group('module'), 'SKPaymentQueue', match.start()):
                    argument = re.match(r'\s*(?:(StoreKit)\s*\.\s*)?(\w+)', code[match.end():])
                    if not argument:
                        continue
                    name = argument.group(2)
                    payment = name in {'SKPayment', 'SKMutablePayment'} and accepted(argument.group(1), name, match.end())
                    if not payment:
                        for declaration in re.finditer(r'\b(?:let|var)\s+' + re.escape(name) + r'\s*(?::\s*(?P<typed>(?:StoreKit\s*\.\s*)?SK(?:Mutable)?Payment)\b|=\s*(?P<init>(?:StoreKit\s*\.\s*)?SK(?:Mutable)?Payment)\s*\()', code):
                            if visible(declaration, match.start()) and declaration.start() < match.start():
                                payment = True
                        for declaration in re.finditer(r'\b' + re.escape(name) + r'\s*:\s*(?:StoreKit\s*\.\s*)?SK(?:Mutable)?Payment\b', code):
                            if visible(declaration, match.start()):
                                payment = True
                    if payment:
                        evidence(match, legacy)
        else:
            imported = bool(re.search(r'@import\s+StoreKit\b|#\s*(?:import|include)\s*<\s*StoreKit/', code))
            if imported:
                for match in re.finditer(r'\[\s*\[\s*SKPaymentQueue\s+defaultQueue\s*\]\s+addPayment\s*:', code):
                    evidence(match, legacy)
    if modern:
        return {'status': 'PASS', 'actual': '发现明确 StoreKit 2 API 使用；' + _NOTICE, 'evidence': modern[:8]}
    if legacy and not scan_incomplete:
        return {'status': 'FAIL', 'actual': '仅发现明确旧版 StoreKit 购买实现，未发现 StoreKit 2 API 使用', 'evidence': legacy[:8]}
    reason = '源码扫描不完整，无法确认 StoreKit 版本' if scan_incomplete else '未发现可确认的 StoreKit 2 API 使用；导入、类型声明、配置或不可读取封装不足以确认实现'
    return {'status': 'NOT_VERIFIABLE', 'actual': reason, 'evidence': legacy[:8]}
