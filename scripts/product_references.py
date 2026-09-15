"""Conservative local references to already identified product catalogues.

This is deliberately not general Swift data-flow analysis. An unresolved value
stays unresolved; a catalogue elsewhere in the project is never a blanket waiver.
"""
from __future__ import annotations

import re

try:
    from .restore_detection import _lex
except ImportError:
    from restore_detection import _lex


def _pairs(code):
    stack, ends = [], {}
    for pos, char in enumerate(code):
        if char in '([{':
            stack.append(pos)
        elif char in ')]}' and stack and code[stack[-1]] == {')': '(', ']': '[', '}': '{'}[char]:
            ends[stack.pop()] = pos
    return ends


def _direct_field(code, ends, opening, closing, position):
    return opening < position < closing and not any(
        opening < child < position < child_end < closing
        for child, child_end in ends.items()
    )


def _parameter_shadow(code, ends, name, after, position):
    """Reject active function/closure binders introduced after the source."""
    escaped = re.escape(name)
    after = max(after, 0)
    for opening, closing in ends.items():
        if code[opening] != '{' or not after < opening < position < closing:
            continue
        signature = re.match(r'\s*(?:\[[^\]]*\]\s*)?(?:\([^)]*\)|(?:\w+\s*,\s*)*\w+)\s+in\b', code[opening + 1:closing])
        if re.search(r'\bfor\s+' + escaped + r'\s+in\b[^{}]*$', code[after:opening]):
            return True
        if signature and re.search(r'\b' + escaped + r'\b', signature.group()):
            return True
        for function in re.finditer(r'(?:\bfunc\s+\w+(?:<[^{}]*>)?|(?<![.\w])init[?!]?|\bsubscript)\s*\(', code[after:opening]):
            params = after + function.end() - 1
            end = ends.get(params)
            if end is not None and end < opening and not re.search(r'[{}]', code[end + 1:opening]):
                if re.search(r'\b' + escaped + r'\s*:', code[params:end]):
                    return True
    return False


class ProductReferences:
    def __init__(self, sources, identified):
        self.sources = {path: _lex(raw)[1] for path, raw in sources.items()}
        self.ends = {path: _pairs(code) for path, code in self.sources.items()}
        self.catalogues = []
        for path, code in self.sources.items():
            positions = identified.get(path, set())
            for match in re.finditer(r'\b(let|var)\s+(\w+)\s*(?::\s*\[[\w.<> ]+\])?\s*=\s*\[', code):
                start = match.end() - 1
                end = self.ends[path].get(start)
                if end is None or match.group(1) != 'let':
                    continue
                remainder = code[end + 1:]
                if re.match(r'\s*[.+?!(\[*/&|^-]', remainder):
                    continue
                same_line = remainder.split('\n', 1)[0].strip()
                if same_line and not same_line.startswith((';', '}')):
                    continue
                # Every direct element must be a recognized initializer call.
                cursor, count, valid = start + 1, 0, True
                elements = []
                while cursor < end:
                    if code[cursor].isspace() or code[cursor] == ',':
                        cursor += 1
                        continue
                    call = re.match(r'(?:[\w.]+|\.init)\s*\(', code[cursor:end])
                    if not call:
                        valid = False
                        break
                    opening = cursor + call.end() - 1
                    closing = self.ends[path].get(opening)
                    if closing is None or not any(
                        _direct_field(code, self.ends[path], opening, closing, pos)
                        for pos in positions
                    ):
                        valid = False
                        break
                    count += 1
                    elements.append((opening, closing))
                    cursor = closing + 1
                if not valid or not count:
                    continue
                # Nested initializers and explicit identity conflicts already
                # went through extraction. Reject any unresolved identity in
                # this array by comparing its original field positions.
                try:
                    from .product_context import identity_fields
                except ImportError:
                    from product_context import identity_fields
                _, fields = identity_fields(sources[path])
                relevant = [f for f in fields if start < f.start < end and (f.label.lower() != 'id' or not f.has_explicit_identity)]
                if any(f.start not in positions for f in relevant):
                    continue
                enclosing = [(a, b) for a, b in self.ends[path].items() if code[a] == '{' and a < match.start() < b]
                owner = None
                if enclosing:
                    a, b = min(enclosing, key=lambda item: item[1] - item[0])
                    heading = re.search(r'\b(?:struct|class|enum)\s+(\w+)[^{]*$', code[:a])
                    if heading and re.search(r'\bstatic\s*$', code[:match.start()]):
                        owner = heading.group(1)
                def direct_fields(a, b):
                    return [f for f in relevant if _direct_field(code, self.ends[path], a, b, f.start)]

                labels = set.intersection(*({f.label.lower() for f in direct_fields(a, b)} for a, b in elements))
                self.catalogues.append((path, match.group(2), owner, match.start(), end, labels))

    def _catalogue(self, path, expression, at, label):
        expression = re.sub(r'\s+', '', expression)
        base = expression.split('.')[0]
        if _parameter_shadow(self.sources[path], self.ends[path], base, -1, at):
            return False
        matches = []
        for item in self.catalogues:
            source, name, owner, begin, end, labels = item
            if label.lower() not in labels:
                continue
            if '.' in expression:
                if owner and sum(len(re.findall(r'\b(?:struct|class|enum)\s+' + re.escape(owner) + r'\b', text)) for text in self.sources.values()) == 1 and expression == owner + '.' + name and not re.search(r'\b(?:let|var)\s+' + re.escape(owner) + r'\b', self.sources[path][:at]):
                    matches.append(item)
            elif source == path and name == expression and begin < at:
                code = self.sources[path]
                if re.search(r'\b(?:let|var)\s+' + re.escape(name) + r'\b|\b' + re.escape(name) + r'\s*=', code[end:at]):
                    continue
                # Binding must live in a containing lexical scope.
                code = self.sources[path]
                scopes = [(a, b) for a, b in self.ends[path].items() if code[a] == '{' and a < begin < b]
                if all(a < at < b for a, b in scopes):
                    matches.append(item)
        return len(matches) == 1

    def is_catalogue_reference(self, path, position, value):
        member = re.fullmatch(r'\s*(\w+)\s*\.\s*(id|productID|productId|product_id)\s*', value)
        if not member:
            return False
        code, ends = self.sources[path], self.ends[path]
        for match in re.finditer(r'\bForEach\s*\(', code):
            opening = match.end() - 1
            closing = ends.get(opening)
            if closing is None:
                continue
            args = code[opening + 1:closing].split(',')[0].strip()
            closure = re.match(r'\s*\{\s*(\w+)\s+in\b', code[closing + 1:])
            if not closure or closure.group(1) != member.group(1):
                continue
            begin = code.index('{', closing + 1)
            end = ends.get(begin, begin)
            if not begin < position < end or not self._catalogue(path, args, match.start(), member.group(2)):
                continue
            # Shadowing/reassignment may alter the bound element. Conservative
            # rejection even when the nested scope would be harmless.
            name = re.escape(member.group(1))
            body_start = closing + 1 + closure.end()
            body = code[body_start:position]
            if _parameter_shadow(code, ends, member.group(1), body_start - 1, position):
                continue
            if re.search(r'\b(?:let|var)\s+' + name + r'\b|\b' + name + r'(?:\s*\.\s*\w+)?\s*=|\{\s*' + name + r'\s+in', body):
                continue
            return True
        return False

    def is_transaction_read(self, path, position, value):
        match = re.fullmatch(r'\s*(\w+)\s*\.\s*payment\s*\.\s*productIdentifier\s*', value)
        if not match:
            return False
        name = re.escape(match.group(1))
        code = self.sources[path]
        # Only a parameter explicitly typed as an SKPaymentTransaction is
        # sufficient evidence; same-spelled application models are not.
        for function in re.finditer(r'\bfunc\s+\w+\s*\(', code):
            opening = function.end() - 1
            closing = self.ends[path].get(opening)
            if closing is None or not re.search(r'\b' + name + r'\s*:\s*SKPaymentTransaction\b', code[opening:closing]):
                continue
            body = re.match(r'[^{}]*\{', code[closing + 1:])
            if body:
                begin = closing + body.end()
                if begin < position < self.ends[path].get(begin, begin):
                    body_code = code[begin + 1:position]
                    if not _parameter_shadow(code, self.ends[path], match.group(1), begin, position) and not re.search(r'\b(?:let|var)\s+' + name + r'\b|\b' + name + r'\s*=', body_code):
                        return True
        return False


def field_expression(clean, field):
    """Get one expression without crossing a sibling field or statement."""
    code = _lex(clean)[1]
    ends = _pairs(code)
    start, pos = field.value_start, field.value_start
    while pos < len(code):
        if code[pos] in ',;)\n}':
            break
        if pos in ends:
            pos = ends[pos] + 1
        else:
            pos += 1
    return clean[start:pos].strip()
