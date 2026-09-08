"""Identify purchase restoration implementation, without treating policy prose as UI."""
from __future__ import annotations

import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET

_SOURCE = {".swift", ".m", ".mm", ".h", ".c", ".cc", ".cpp"}
_LABEL = re.compile(r"(?:restore\s+(?:purchases?|transactions?)|恢复购买|恢復購買|恢复内购|恢復內購)", re.I)
_API = re.compile(r"\b(?:restorePurchases|restoreCompletedTransactions)\s*\(|\bAppStore\s*\.\s*sync\s*\(")
# Objective-C uses selectors rather than parenthesized calls.
_OBJC_API = re.compile(r"\b(?:restorePurchases|restoreCompletedTransactions)\s*(?=\]|;|\{|:)")


def _mask(text: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in text)


def _lex(raw: str) -> tuple[str, str, list[tuple[int, int, str]]]:
    """Return comment-free text, code-only text, and literal spans with stable offsets.

    Swift multiline/raw strings are consumed atomically, so examples of API calls
    inside agreements cannot become executable-code evidence.
    """
    comments = list(raw)
    code = list(raw)
    literals: list[tuple[int, int, str]] = []
    i = 0
    while i < len(raw):
        start = i
        if raw.startswith("//", i):
            end = raw.find("\n", i)
            i = len(raw) if end < 0 else end
        elif raw.startswith("/*", i):
            i += 2
            depth = 1
            while i < len(raw) and depth:
                if raw.startswith("/*", i):
                    depth += 1
                    i += 2
                elif raw.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        else:
            opening = re.match(r'(#*)("""|"|\')', raw[i:])
            if not opening:
                i += 1
                continue
            hashes, quote = opening.groups()
            body_start = i + len(opening.group())
            delimiter = quote + hashes
            i = body_start
            while i < len(raw):
                if raw.startswith("\\" + hashes, i):
                    i += 2 + len(hashes)
                elif raw.startswith(delimiter, i):
                    body = raw[body_start:i]
                    i += len(delimiter)
                    literals.append((start, i, body))
                    break
                else:
                    i += 1
            code[start:i] = _mask(raw[start:i])
            continue
        comments[start:i] = _mask(raw[start:i])
        code[start:i] = _mask(raw[start:i])
    return "".join(comments), "".join(code), literals


def detect_restore(path: Path, raw: str) -> list[dict]:
    """Return definite restore API / purchase restore control evidence only.

    Ordinary documents, agreements, comments and generic backup restore operations
    are not restoration functionality. No sentence-level negation filter is used:
    a disclaimer cannot hide executable restore code on the same line.
    """
    offsets: set[int] = set()
    suffix = path.suffix.lower()
    if suffix in _SOURCE:
        clean, code, literals = _lex(raw)
        offsets.update(m.start() for m in _API.finditer(code))
        if suffix in {".m", ".mm", ".h"}:
            offsets.update(m.start() for m in _OBJC_API.finditer(code))
        for start, end, body in literals:
            if not _LABEL.fullmatch(body.strip()):
                continue
            before = clean[max(0, start - 250):start]
            # Direct SwiftUI button title, UIKit titles.
            direct = re.search(r'(?:\bButton\s*\(|\bsetTitle\s*\(\s*@?|\bsetTitle\s*:\s*@?|\btitle\s*=\s*)$', before)
            # SwiftUI trailing label closure: Button { action } label: { Text(...).
            label = re.search(r'\bButton\b[^;]*\blabel\s*:\s*\{\s*(?:Label|Text)\s*\(\s*$', before, re.S)
            trailing = re.search(r'\bButton\s*\([^;]*\)\s*\{\s*(?:Label|Text)\s*\(\s*$', before, re.S)
            if direct or label or trailing:
                offsets.add(start)
    elif suffix == ".strings":
        clean, _, literals = _lex(raw)
        # Only values are UI labels; a key named restorePurchases proves nothing.
        for start, end, body in literals:
            if _LABEL.fullmatch(body.strip()) and re.search(r'=\s*$', clean[:start]):
                offsets.add(start)
    elif suffix == ".xcstrings":
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = {}
        def visit(value: object) -> None:
            if isinstance(value, dict):
                unit = value.get("stringUnit")
                if isinstance(unit, dict) and isinstance(unit.get("value"), str) and _LABEL.fullmatch(unit["value"].strip()):
                    for ensure_ascii in (False, True):
                        literal = json.dumps(unit["value"], ensure_ascii=ensure_ascii)
                        match = re.search(re.escape(literal), raw)
                        if match:
                            offsets.add(match.start())
                            break
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(data)
    elif suffix in {".storyboard", ".xib"}:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            root = None
        if root is not None:
            xml_text = re.sub(r"<!--.*?-->", lambda m: _mask(m.group()), raw, flags=re.S)
            for control in root.iter():
                if control.tag not in {"button", "barButtonItem", "menuItem"}:
                    continue
                for node in control.iter():
                    for key in ("title", "text"):
                        value = node.get(key, "")
                        if _LABEL.fullmatch(value.strip()):
                            match = re.search(r'\b' + key + r'\s*=\s*[\"\']' + re.escape(value), xml_text)
                            if match:
                                offsets.add(match.start())
    lines = raw.splitlines()
    evidence = []
    seen_lines: set[int] = set()
    for offset in sorted(offsets):
        line = raw.count("\n", 0, offset) + 1
        if line not in seen_lines:
            evidence.append({"line": line, "excerpt": lines[line - 1].strip()[:300]})
            seen_lines.add(line)
    return evidence
