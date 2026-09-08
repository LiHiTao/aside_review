"""Balanced, literal-aware local record contexts for product identity scanning."""
from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from .restore_detection import _lex
except ImportError:
    from restore_detection import _lex


@dataclass
class IdentityField:
    label: str
    start: int
    value_start: int
    context: str
    initializer: str
    has_explicit_identity: bool


def _without_generic_arguments(header: str) -> str:
    """Remove one balanced trailing generic clause before a parameter list."""
    header = header.rstrip()
    if not header.endswith(">"):
        return header
    depth = 0
    for pos in range(len(header) - 1, -1, -1):
        if header[pos] == ">":
            depth += 1
        elif header[pos] == "<":
            depth -= 1
            if depth == 0:
                return header[:pos].rstrip()
    return header


def identity_fields(raw: str) -> tuple[str, list[IdentityField]]:
    clean, code, _ = _lex(raw)
    stack: list[int] = []
    parents: dict[int, int | None] = {}
    ends: dict[int, int] = {}
    owner: dict[int, int | None] = {}
    for pos, char in enumerate(code):
        owner[pos] = stack[-1] if stack else None
        if char in "([{":
            parents[pos] = stack[-1] if stack else None
            stack.append(pos)
        elif char in ")]}":
            if stack and code[stack[-1]] == {")": "(", "]": "[", "}": "{"}[char]:
                ends[stack.pop()] = pos
    pattern = re.compile(r"\b(?P<label>id|productid|product_id)\b\s*(?P<op>[:=])\s*", re.I)
    matches = []
    for match in pattern.finditer(code):
        before = code[:match.start()]
        enclosing = owner[match.start()]
        # Type annotations and callable declarations define storage/parameters,
        # not catalogue values. Existing literal assignments remain detectable.
        if match.group("op") == ":":
            if re.search(r"\b(?:let|var)\s*$", before):
                continue
            if enclosing is not None and code[enclosing] == "(":
                header = _without_generic_arguments(code[:enclosing])
                if re.search(r"(?:\bfunc\s+[A-Za-z_][A-Za-z_0-9]*|\binit[?!]?)\s*$", header):
                    continue
        matches.append(match)
    records = {}
    for match in matches:
        enclosing = owner[match.start()]
        # Only direct named initializer/tuple fields share identity authority.
        # Independent assignments in a function/class never suppress each other.
        record = enclosing if match.group("op") == ":" and enclosing is not None and code[enclosing] == "(" and enclosing in ends else None
        records[match.start()] = record
    explicit_records = {records[m.start()] for m in matches if m.group("label").lower() != "id" and records[m.start()] is not None}
    fields = []
    for match in matches:
        record = records[match.start()]
        if record is None:
            begin = code.rfind("\n", 0, match.start()) + 1
            end = code.find("\n", match.start())
            end = len(code) if end < 0 else end
            initializer = ""
        else:
            begin, end = record + 1, ends[record]
            name = re.search(r"([A-Za-z_][A-Za-z_0-9]*)\s*$", _without_generic_arguments(code[:record]))
            initializer = name.group(1) if name else ""
        # Hide child expressions, preventing nested prices/labels leaking out.
        context = list(clean[begin:end])
        for child, parent in parents.items():
            if parent == record and child in ends and begin <= child < end:
                a, b = child - begin, min(ends[child] + 1, end) - begin
                context[a:b] = " " * (b - a)
        # Whitespace in code includes masked literals: locate the expression in
        # comment-free text instead of using the regex's greedy end offset.
        op = re.search(r"[:=]", code[match.start():match.end()])
        value_start = match.start() + op.end()
        while value_start < len(clean) and clean[value_start].isspace():
            value_start += 1
        fields.append(IdentityField(match.group("label"), match.start(), value_start, "".join(context), initializer, record in explicit_records))
    return clean, fields
