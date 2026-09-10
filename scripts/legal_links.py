"""Find WKWebView loading calls associated with legal-document UI entries.

Balanced source scopes associate an entry with its page or called helpers. URL
literals, view mounting and complete runtime control flow are not prerequisites.
Unrelated WebViews, prose and uncalled helper bodies do not supply evidence.
No code is executed and no network request is made here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import plistlib
import json
import re
from typing import Mapping

try:
    from .restore_detection import _lex
except ImportError:
    from restore_detection import _lex


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    literal: str | None = None


@dataclass
class Source:
    path: Path
    raw: str
    tokens: list[Token] = field(default_factory=list)
    pairs: dict[int, int] = field(default_factory=dict)
    incomplete: bool = False

    def __post_init__(self):
        clean, code, literals = _lex(self.raw)
        spans = {start: (end, value) for start, end, value in literals}
        cursor = 0
        while cursor < len(self.raw):
            if cursor in spans:
                cursor = spans[cursor][0]
            elif self.raw.startswith("//", cursor):
                newline = self.raw.find("\n", cursor)
                cursor = len(self.raw) if newline < 0 else newline
            elif self.raw.startswith("/*", cursor):
                cursor += 2
                depth = 1
                while cursor < len(self.raw) and depth:
                    if self.raw.startswith("/*", cursor):
                        depth += 1
                        cursor += 2
                    elif self.raw.startswith("*/", cursor):
                        depth -= 1
                        cursor += 2
                    else:
                        cursor += 1
                self.incomplete |= bool(depth)
            else:
                cursor += 1
        pattern = re.compile(r"[A-Za-z_$][\w$]*|[0-9]+|[^\s]", re.UNICODE)
        i = 0
        while i < len(clean):
            if i in spans:
                end, value = spans[i]
                self.tokens.append(Token(clean[i:end], i, value))
                i = end
            elif clean[i].isspace():
                i += 1
            else:
                match = pattern.match(clean, i)
                if match:
                    self.tokens.append(Token(match.group(), i))
                    i = match.end()
                else:
                    i += 1
        stack = []
        for i, token in enumerate(self.tokens):
            if token.literal is not None:
                continue
            if token.text in ("(", "[", "{"):
                stack.append(i)
            elif token.text in (")", "]", "}"):
                if not stack or self.tokens[stack[-1]].text != {")": "(", "]": "[", "}": "{"}[token.text]:
                    self.incomplete = True
                else:
                    begin = stack.pop()
                    self.pairs[begin] = i
                    self.pairs[i] = begin
        self.incomplete |= bool(stack)
        # _lex intentionally tolerates malformed source; a dangling string is
        # not allowed to make the legal scan look complete.
        self.incomplete |= any(t.text in ('"', "'") and t.literal is None for t in self.tokens)

    def text(self, begin, end):
        return "".join(t.text for t in self.tokens[begin:end])

    def ancestors(self, index):
        return sorted((i for i, j in self.pairs.items()
                       if i < index < j and self.tokens[i].text == "{"), reverse=True)


@dataclass
class Definition:
    source: Source
    name: str
    start: int
    begin: int
    end: int
    owner: str | None
    params: list[tuple[str, str, str]] = field(default_factory=list)
    kind: str = "func"


@dataclass
class Value:
    text: str | None = None
    type_name: str | None = None
    args: dict[str, "Value"] = field(default_factory=dict)


def _kind(label):
    label = re.sub(r"[_\-]+", " ", label).strip().lower()
    if re.fullmatch(r"(?:privacy(?: policy| agreement| notice)?|隐私(?:政策|协议|条款)|隱私(?:政策|協議|條款))", label):
        return "privacy"
    if re.fullmatch(r"(?:terms(?: of (?:service|use))?|user agreement|user terms|terms (?:and|&) (?:conditions|support)|eula|用户(?:协议|条款)|使用(?:协议|条款)|用戶(?:協議|條款))", label):
        return "terms"
    return None


def _split(source, begin, end, delimiter=","):
    result, part, i = [], begin, begin
    while i < end:
        if source.tokens[i].text == delimiter:
            result.append((part, i))
            part = i + 1
        elif i in source.pairs and source.pairs[i] > i:
            i = source.pairs[i]
        i += 1
    if part < end:
        result.append((part, end))
    return result


def _args(source, begin, end):
    result = []
    for a, b in _split(source, begin, end):
        if a + 1 < b and source.tokens[a + 1].text == ":":
            result.append((source.tokens[a].text, a + 2, b))
        else:
            result.append(("_", a, b))
    return result


def _objc_call(source, begin, end):
    """Split one bracket message, skipping nested receivers and arguments."""
    if begin >= end or source.tokens[begin].text != "[" or source.pairs.get(begin) != end - 1:
        return None
    i = begin + 1
    top = []
    while i < end - 1:
        top.append(i)
        if i in source.pairs and source.pairs[i] > i:
            i = source.pairs[i]
        i += 1
    selectors = [i for i in top if i + 1 < end - 1 and source.tokens[i + 1].text == ":"]
    if selectors:
        first = selectors[0]
        args = []
        for n, index in enumerate(selectors):
            stop = selectors[n + 1] if n + 1 < len(selectors) else end - 1
            args.append((source.tokens[index].text, index + 2, stop))
        return source.tokens[first].text, (begin + 1, first), args
    if len(top) >= 2:
        return source.tokens[top[-1]].text, (begin + 1, top[-1]), []
    return None


def _end_expr(source, begin, limit):
    i = begin
    while i < limit:
        if source.tokens[i].text in (";", "}", ","):
            break
        if i > begin:
            gap = source.raw[source.tokens[i - 1].start + len(source.tokens[i - 1].text):source.tokens[i].start]
            if "\n" in gap and source.tokens[i].text != "." and source.tokens[i - 1].text not in ("=", ".", "+", "?", ":"):
                break
        if i in source.pairs and source.pairs[i] > i:
            i = source.pairs[i]
        i += 1
    return i


def _simple_receiver(source, index):
    """Read a local/self property receiver, including optional/forced chains."""
    if index < 2 or source.tokens[index - 1].text != ".":
        return None
    j = index - 2
    while j >= 0 and source.tokens[j].text in ("?", "!"):
        j -= 1
    if j < 0 or not re.fullmatch(r"[A-Za-z_]\w*", source.tokens[j].text):
        return None
    if j >= 2 and source.tokens[j - 1].text == "." and source.tokens[j - 2].text != "self":
        return None
    return source.tokens[j].text


def _control_bodies(source, begin, end):
    result = set()
    for i in range(begin, end):
        if source.tokens[i].text in ("if", "else", "guard", "switch", "for", "while", "do", "catch"):
            opening = next((j for j in range(i + 1, end) if source.tokens[j].text == "{"), None)
            if opening in source.pairs:
                result.add(opening)
    return result


def _execution_indices(source, begin, end):
    """Visit ordinary control blocks, excluding nested declarations/closures."""
    controls = _control_bodies(source, begin, end)
    i = begin
    while i < end:
        if source.tokens[i].text == "func":
            opening = next((j for j in range(i + 1, end) if source.tokens[j].text == "{"), None)
            if opening in source.pairs:
                i = source.pairs[opening] + 1
                continue
        if source.tokens[i].text == "{" and i in source.pairs:
            if i in controls:
                yield from _execution_indices(source, i + 1, source.pairs[i])
            i = source.pairs[i] + 1
            continue
        yield i
        i += 1


def _switches(source, begin, end):
    """Yield balanced switch expressions and their immediate case bodies."""
    for i in _execution_indices(source, begin, end):
        if source.tokens[i].text != "switch":
            continue
        opening = next((j for j in range(i + 1, end) if source.tokens[j].text == "{"), None)
        if opening not in source.pairs:
            continue
        close = source.pairs[opening]
        cases = []
        j = opening + 1
        while j < close:
            if source.tokens[j].text in ("case", "default"):
                colon = next((k for k in range(j + 1, close) if source.tokens[k].text == ":"), None)
                if colon is None:
                    break
                cases.append((j + 1, colon, colon + 1))
                j = colon + 1
                continue
            if j in source.pairs and source.pairs[j] > j:
                j = source.pairs[j]
            j += 1
        spans = [(a, b, c, cases[n + 1][0] - 1 if n + 1 < len(cases) else close)
                 for n, (a, b, c) in enumerate(cases)]
        yield source.text(i + 1, opening), spans


def _row_predicate(expression, parameter):
    match = re.fullmatch(re.escape(parameter) + r"\.(row|section)(==|!=)(\d+)", expression)
    return (match[1], match[2] + match[3]) if match else None


class Analyzer:
    def __init__(self, root, source_texts, all_texts, scan_incomplete):
        self.root = root.resolve()
        self.sources = [Source(p, text) for p, text in source_texts.items()
                        if p.suffix.lower() in {".swift", ".m", ".mm", ".h"}]
        self.incomplete = scan_incomplete or not self.sources or any(s.incomplete for s in self.sources)
        self.definitions: list[Definition] = []
        self.constants = []
        self.entries = []
        self.entry_locations = set()
        self.objc_bases = {}
        self.info_values = {}
        self.localized_kinds = {}
        for path, raw in all_texts.items():
            if path.suffix == ".strings":
                localization = Source(path, raw.lstrip("\ufeff"))
                self.incomplete |= localization.incomplete
                tokens = localization.tokens
                i = 0
                while i < len(tokens):
                    if (i + 3 >= len(tokens) or tokens[i].literal is None
                            or tokens[i + 1].text != "=" or tokens[i + 2].literal is None
                            or tokens[i + 3].text != ";"):
                        self.incomplete = True
                        break
                    key, label = tokens[i].literal, tokens[i + 2].literal
                    if _kind(label):
                        self.localized_kinds.setdefault(key, set()).add(_kind(label))
                    i += 4
            elif path.suffix == ".xcstrings":
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict) or not isinstance(data.get("strings"), dict):
                        raise ValueError("Invalid strings catalogue")
                    for key, item in data["strings"].items():
                        if not isinstance(item, dict) or not isinstance(item.get("localizations", {}), dict):
                            raise ValueError("Invalid localization record")
                        def collect_units(node):
                            if not isinstance(node, dict):
                                raise ValueError("Invalid localization unit")
                            if "stringUnit" in node:
                                unit = node["stringUnit"]
                                if not isinstance(unit, dict) or not isinstance(unit.get("value"), str):
                                    raise ValueError("Invalid string unit")
                                label = unit["value"]
                                if _kind(label):
                                    self.localized_kinds.setdefault(key, set()).add(_kind(label))
                            for name, child in node.items():
                                if name != "stringUnit" and isinstance(child, dict):
                                    collect_units(child)
                        for localization in item.get("localizations", {}).values():
                            collect_units(localization)
                except (ValueError, TypeError, AttributeError):
                    self.incomplete = True
            if path.name != "Info.plist":
                continue
            try:
                values = plistlib.loads(raw.encode("utf-8"))
            except (ValueError, TypeError, plistlib.InvalidFileException):
                continue
            if isinstance(values, dict):
                for key, val in values.items():
                    if isinstance(val, str):
                        self.info_values.setdefault(key, []).append(val)
        self._index()

    def label_kind(self, label):
        kind = _kind(label)
        candidates = self.localized_kinds.get(label, set())
        return kind or (next(iter(candidates)) if len(candidates) == 1 else None)

    def expression_kind(self, s, a, b):
        value = self.value(s, a, b)
        if value.text:
            kind = self.label_kind(value.text)
            if kind:
                return kind
        if a + 2 < b and s.tokens[a].text in ("NSLocalizedString", "String"):
            literals = [t.literal for t in s.tokens[a:b] if t.literal is not None]
            if literals:
                return self.label_kind(literals[0])
        return None

    def evidence(self, source, index):
        token = source.tokens[min(index, len(source.tokens) - 1)]
        line = source.raw.count("\n", 0, token.start) + 1
        try:
            path = str(source.path.resolve().relative_to(self.root))
        except ValueError:
            # A caller may provide relative paths; never emit an absolute path.
            path = str(source.path) if not source.path.is_absolute() else source.path.name
        return {"path": path, "line": line, "excerpt": source.raw.splitlines()[line - 1].strip()[:300]}

    def _index(self):
        for s in self.sources:
            types = []
            for i, token in enumerate(s.tokens):
                if token.text in ("struct", "class", "enum", "extension") and i + 1 < len(s.tokens):
                    j = i + 2
                    while j < len(s.tokens) and s.tokens[j].text not in ("{", ";", "}"):
                        j += 1
                    if j in s.pairs:
                        definition = Definition(s, s.tokens[i + 1].text, i, j + 1, s.pairs[j], None, kind="type")
                        types.append(definition)
                        self.definitions.append(definition)
                if token.text == "interface" and i > 0 and s.tokens[i - 1].text == "@" and i + 3 < len(s.tokens) and s.tokens[i + 2].text == ":":
                    self.objc_bases[s.tokens[i + 1].text] = s.tokens[i + 3].text
                if token.text == "implementation" and i > 0 and s.tokens[i - 1].text == "@" and i + 1 < len(s.tokens):
                    end = next((j for j in range(i + 2, len(s.tokens) - 1)
                                if s.tokens[j].text == "@" and s.tokens[j + 1].text == "end"), len(s.tokens))
                    definition = Definition(s, s.tokens[i + 1].text, i, i + 2, end, None, kind="type")
                    types.append(definition)
                    self.definitions.append(definition)
            for i, token in enumerate(s.tokens):
                owner = next((d.name for d in reversed(types) if d.begin <= i < d.end), None)
                paren = i + (1 if token.text == "init" else 2)
                declaration = token.text == "func" or (token.text == "init" and (i == 0 or s.tokens[i - 1].text != "."))
                if declaration and paren < len(s.tokens) and s.tokens[paren].text == "(" and paren in s.pairs:
                    close = s.pairs[paren]
                    j = close + 1
                    while j < len(s.tokens) and s.tokens[j].text not in ("{", "}", ";", "func"):
                        j += 1
                    if j not in s.pairs or s.tokens[j].text != "{":
                        continue
                    params = []
                    for a, b in _split(s, paren + 1, close):
                        colon = next((k for k in range(a, b) if s.tokens[k].text == ":"), None)
                        if colon is not None:
                            names = [t.text for t in s.tokens[a:colon]]
                            params.append((names[0], names[-1], s.text(colon + 1, b)))
                    self.definitions.append(Definition(s, "init" if token.text == "init" else s.tokens[i + 1].text, i, j + 1, s.pairs[j], owner, params))
                if token.text in ("let", "var", "case") and i + 1 < len(s.tokens):
                    j = i + 2
                    while j < len(s.tokens) and s.tokens[j].text not in ("=", "{", "}", ";", ","):
                        if "\n" in s.raw[s.tokens[j - 1].start:s.tokens[j].start]:
                            break
                        j += 1
                    if j < len(s.tokens) and s.tokens[j].text == "=":
                        self.constants.append((s, s.tokens[i + 1].text, owner, i, j + 1, _end_expr(s, j + 1, len(s.tokens))))
            # Basic no-argument Objective-C action methods.
            for i in range(len(s.tokens) - 5):
                if s.tokens[i].text in ("-", "+") and s.tokens[i + 1].text == "(" and i + 1 in s.pairs:
                    close = s.pairs[i + 1]
                    if close + 2 < len(s.tokens) and s.tokens[close + 2].text == "{" and close + 2 in s.pairs:
                        self.definitions.append(Definition(s, s.tokens[close + 1].text, i,
                            close + 3, s.pairs[close + 2], owner=next((d.name for d in reversed(types) if d.begin <= i < d.end), None)))

    def owner(self, s, index):
        types = [d for d in self.definitions if d.kind == "type" and d.source is s and d.begin <= index < d.end]
        return min(types, key=lambda d: d.end - d.begin).name if types else None

    def value(self, s, a, b, env=None, seen=None):
        env = env or {}
        seen = seen or set()
        while a < b and s.tokens[a].text in ("&", "@", "try", "await"):
            a += 1
        while a < b and s.tokens[b - 1].text in ("!", "?"):
            b -= 1
        if b >= a + 3 and s.tokens[b - 1].text == "String":
            if s.tokens[b - 2].text in ("?", "!") and s.tokens[b - 3].text == "as":
                b -= 3
            elif s.tokens[b - 2].text == "as":
                b -= 2
        if a >= b:
            return Value()
        if s.tokens[a].text == "(" and s.pairs.get(a) == b - 1:
            return self.value(s, a + 1, b - 1, env, seen)
        if b == a + 1 and s.tokens[a].literal is not None:
            literal = s.tokens[a].literal
            return Value(text=None if re.search(r"\\#*\(", literal) else literal)
        # Swift constructors URL(string:), URLRequest(url:) and typed wrappers.
        opening = next((i for i in range(a, b) if s.tokens[i].text == "("), None)
        if opening is not None and s.pairs.get(opening) == b - 1:
            name = s.text(a, opening)
            args = {label: self.value(s, x, y, env, seen.copy()) for label, x, y in _args(s, opening + 1, b - 1)}
            if name in ("URL", "NSURL"):
                if "fileURLWithPath" in args:
                    v = args["fileURLWithPath"]
                    return Value(text="file://" + (v.text or ""))
                return args.get("string", Value())
            if name == "URLRequest":
                return args.get("url", Value())
            if name == "Bundle.main.object":
                key = args.get("forInfoDictionaryKey", Value()).text
                values = self.info_values.get(key, [])
                return Value(text=values[0]) if len(values) == 1 else Value()
            return Value(type_name=name, args=args)
        # Objective-C NSURL / NSURLRequest expressions; brackets are balanced.
        if s.tokens[a].text == "[" and s.pairs.get(a) == b - 1:
            if s.path.suffix == ".swift":
                return Value(type_name="Array", args={str(i): self.value(s, x, y, env, seen.copy()) for i, (x, y) in enumerate(_split(s, a + 1, b - 1))})
            call = _objc_call(s, a, b)
            if call:
                selector, receiver, arguments = call
                args = {label: self.value(s, x, y, env, seen.copy()) for label, x, y in arguments}
                if selector in ("URLWithString", "requestWithURL", "fileURLWithPath"):
                    v = args.get(selector, Value())
                    return Value(text="file://" + (v.text or "")) if selector == "fileURLWithPath" else v
                if selector == "alloc":
                    return Value(type_name=s.text(*receiver))
                if selector.startswith("init"):
                    allocated = self.value(s, *receiver, env, seen)
                    if selector in ("initWithURL", "initWithUrl"):
                        return Value(type_name=allocated.type_name, args={"url": args.get(selector, Value())})
                    if selector in ("init", "initWithFrame"):
                        return allocated
                    if selector == "initWithRootViewController":
                        return Value(type_name=allocated.type_name, args={"rootViewController": args.get(selector, Value())})
        name = s.text(a, b)
        for prefix in ("self.", "$", "&"):
            if name.startswith(prefix):
                name = name[len(prefix):]
        if name.endswith(".rawValue"):
            name = name[:-9]
        if name in env:
            return env[name]
        if "." in name:
            base, prop = name.rsplit(".", 1)
            if base in env and prop in env[base].args:
                return env[base].args[prop]
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name):
            return Value()
        key = (id(s), a, name)
        if key in seen:
            return Value()
        seen.add(key)
        short = name.rsplit(".", 1)[-1]
        qualifier = name.rsplit(".", 1)[0] if "." in name else None
        owner = self.owner(s, a)
        candidates = [c for c in self.constants if c[1] == short and (qualifier is None or c[2] == qualifier)]
        # Lexically visible declarations win; sibling function locals never do.
        visible = []
        ancestors = s.ancestors(a)
        for c in candidates:
            cs, _, co, ci, _, _ = c
            declaration_scopes = cs.ancestors(ci)
            if cs is s and ci < a and all(scope in ancestors for scope in declaration_scopes):
                visible.append(c)
        if visible:
            depth = max(len(s.ancestors(c[3])) for c in visible)
            candidates = [c for c in visible if len(s.ancestors(c[3])) == depth]
        else:
            candidates = [c for c in candidates if not any(d.kind == "func" and d.source is c[0] and d.begin <= c[3] < d.end for d in self.definitions)
                          and (qualifier is not None or c[2] in (None, owner))]
        if len(candidates) == 1:
            cs, _, _, _, ca, cb = candidates[0]
            return self.value(cs, ca, cb, env, seen)
        return Value()

    def outcome(self, status, actual, url=None, evidence=None):
        return {"status": status, "actual": actual, "url": url, "evidence": evidence or [],
                "manual_check": "核对协议入口关联的页面或共用封装是否存在 WKWebView 加载调用。" if status == "NOT_VERIFIABLE" else None}

    def loaded(self, value, s, index):
        return self.outcome("PASS", "协议关联页面或已调用方法中存在 WKWebView 加载调用。", value.text, [self.evidence(s, index)])

    def _bind(self, definition, arguments):
        env = dict(arguments)
        for label, name, type_name in definition.params:
            if label in arguments:
                env[name] = arguments[label]
            elif "_" in arguments and (label == "_" or len(definition.params) == 1):
                env[name] = arguments["_"]
            elif type_name.startswith("WKWebView"):
                env[name] = Value(type_name="WKWebView")
        return env

    def destination(self, value, s, index, seen):
        if value.type_name in ("UINavigationController", "UIKit.UINavigationController"):
            root = value.args.get("rootViewController", Value())
            if root.type_name is None:
                return [self.outcome("NOT_VERIFIABLE", "已关联 UINavigationController 展示，但 rootViewController 为空、动态生成或无法解析。", evidence=[self.evidence(s, index)])]
            if root.type_name in ("UINavigationController", "UIKit.UINavigationController"):
                return [self.outcome("NOT_VERIFIABLE", "发现嵌套 UINavigationController，当前静态模式不确认其实际展示的根页面。", evidence=[self.evidence(s, index)])]
            results = self.destination(root, s, index, seen)
            for result in results:
                result.setdefault("_containers", []).append(value)
            return results or [self.outcome("NOT_VERIFIABLE", "已关联 UINavigationController 的根页面，但根页面的协议加载链无法解析。", evidence=[self.evidence(s, index)])]
        if value.type_name in ("SFSafariViewController", "SafariView", "SafariViewController"):
            return [self.outcome("FAIL", "协议通过 Safari 页面打开，未使用 WKWebView。", value.args.get("url", Value()).text, [self.evidence(s, index)])]
        definitions = [d for d in self.definitions if d.kind == "type" and d.name == value.type_name]
        if len(definitions) != 1:
            return []
        d = definitions[0]
        header = d.source.text(d.start, d.begin)
        if not any(base in header for base in ("UIViewRepresentable", "UIViewControllerRepresentable", "UIViewController", ":View", ":SwiftUI.View")) and self.objc_bases.get(d.name) != "UIViewController":
            return []
        key = (d.source.path, d.start)
        if key in seen:
            return []
        seen = seen | {key}
        properties = dict(value.args)
        constructor = None
        arguments = {}
        initializers = [f for f in self.definitions if f.kind == "func" and f.source is d.source and f.owner == d.name and f.name == "init"]
        if initializers:
            matching = [f for f in initializers if set(value.args) <= {label for label, _, _ in f.params}]
            if len(matching) == 1:
                constructor = matching[0]
                arguments = self._bind(constructor, value.args)
                cs = constructor.source
                for j in range(constructor.begin, constructor.end - 4):
                    if cs.text(j, j + 2) == "self." and cs.tokens[j + 3].text == "=":
                        properties[cs.tokens[j + 2].text] = self.value(cs, j + 4, _end_expr(cs, j + 4, constructor.end), arguments)
        methods = [f for f in self.definitions if f.kind == "func" and f.source is d.source and f.owner == d.name
                   and f.name in ("makeUIView", "updateUIView", "makeUIViewController", "updateUIViewController", "viewDidLoad", "loadView")]
        results = []
        if constructor:
            results += self.trace(constructor.source, constructor.begin, constructor.end,
                                  arguments, seen, destination=True)
        for method in methods:
            results += self.trace(method.source, method.begin, method.end,
                                  self._bind(method, properties), seen, destination=True)
        # SwiftUI view composition (NavigationLink -> wrapper View -> representable).
        if not methods:
            for j in range(d.begin, d.end - 2):
                if d.source.tokens[j].text == "var" and d.source.tokens[j + 1].text == "body":
                    brace = next((k for k in range(j + 2, d.end) if d.source.tokens[k].text == "{"), None)
                    if brace in d.source.pairs:
                        results += self.trace(d.source, brace + 1, d.source.pairs[brace], properties, seen,
                                              destination=True)
        displayed_kind = self._displayed_protocol(properties, methods, constructor, arguments)
        if displayed_kind:
            if not results:
                results = [self.outcome("NOT_VERIFIABLE", "已关联协议页面的实际标题，但该页面的 WKWebView 加载链无法解析。", evidence=[self.evidence(s, index)])]
            for result in results:
                result["_protocol_kind"] = displayed_kind
        return results

    def _displayed_protocol(self, properties, methods, constructor=None, constructor_arguments=None):
        """Bind a generic Support entry only to a real destination's UI title.

        A heading argument that is merely passed or stored is insufficient. The
        bounded proof accepts an unambiguous title/navigationItem.title assignment
        in the presented controller's initializer or lifecycle method.
        """
        title = None
        navigation_title = None
        navigation_title_assigned = False
        ordered = ([constructor] if constructor else []) + sorted(methods, key=lambda method: method.name != "loadView")
        for method in ordered:
            if method.name not in ("init", "viewDidLoad", "loadView"):
                continue
            s = method.source
            env = dict(constructor_arguments or {}) if method is constructor else dict(properties)
            local_names = {name for _, name, _ in method.params}
            i = method.begin
            while i < method.end - 1:
                if s.tokens[i].text == "{" and i in s.pairs:
                    i = s.pairs[i] + 1
                    continue
                if s.tokens[i].text in ("let", "var"):
                    local_names.add(s.tokens[i + 1].text)
                if s.tokens[i + 1].text == "=":
                    end = _end_expr(s, i + 2, method.end)
                    val = self.value(s, i + 2, end, env)
                    start = i
                    while start >= method.begin + 2 and s.tokens[start - 1].text == ".":
                        start -= 2
                    lhs = s.text(start, i + 1)
                    if lhs in ("self.title", "title") and not (lhs == "title" and "title" in local_names):
                        title = val
                    elif lhs in ("navigationItem.title", "self.navigationItem.title") and not (lhs.startswith("navigationItem") and "navigationItem" in local_names):
                        navigation_title = val
                        navigation_title_assigned = True
                    env[s.tokens[i].text] = val
                    i = end
                    continue
                i += 1
        displayed = navigation_title if navigation_title_assigned else title
        return self.label_kind(displayed.text) if displayed and displayed.text else None

    def _modifiers(self, s, index):
        seen = set()
        ends = []
        direct = index + 1
        if direct < len(s.tokens) and s.tokens[direct].text == "(" and direct in s.pairs:
            direct = s.pairs[direct] + 1
            if direct < len(s.tokens) and s.tokens[direct].text == "{" and direct in s.pairs:
                direct = s.pairs[direct] + 1
            ends.append(direct)
        ends += [s.pairs[opening] + 1 for opening in s.ancestors(index)]
        for j in ends:
            while j + 2 < len(s.tokens) and s.tokens[j].text == "." and s.tokens[j + 2].text == "(" and j + 2 in s.pairs:
                close = s.pairs[j + 2]
                end = close + 1
                if end < len(s.tokens) and s.tokens[end].text == "{" and end in s.pairs:
                    end = s.pairs[end] + 1
                if j not in seen:
                    seen.add(j)
                    yield s.tokens[j + 1].text, j + 3, close, close + 1, end
                j = end

    def _override(self, s, index):
        for name, a, b, _, _ in self._modifiers(s, index):
            if name != "environment":
                continue
            args = _args(s, a, b)
            if len(args) != 2 or s.text(args[0][1], args[0][2]) != "\\.openURL":
                continue
            x, y = args[1][1:]
            if x + 1 < y and s.tokens[x].text == "OpenURLAction" and s.tokens[x + 1].text == "{" and x + 1 in s.pairs:
                opening, close = x + 1, s.pairs[x + 1]
                inside = next((i for i in range(opening + 1, close) if s.tokens[i].text == "in"), None)
                if inside:
                    return opening + 1, close, s.tokens[opening + 1].text, inside + 1
        return None

    def _override_trace(self, s, override, value, env, seen, entry_index):
        _, close, param, body = override
        code = s.text(body, close)
        if re.search(r"return\.systemAction\b", code):
            return [self.outcome("FAIL", "协议 openURL 自定义处理返回 systemAction，仍交由系统浏览器打开。", value.text)]
        return self.trace(s, body, close, {**env, param: value}, seen, modifier_index=entry_index)

    def trace(self, s, a, b, env=None, seen=None, destination=False, modifier_index=None):
        env = dict(env or {})
        seen = seen or set()
        results = []
        # Evaluate assignments in execution order. In particular, a URL changed
        # after a load/presentation must never be retroactively used for it.
        assignments = set()
        # A typed WKWebView property is evidence only in the current destination.
        if destination:
            owner = self.owner(s, a)
            types = [d for d in self.definitions if d.kind == "type" and d.name == owner and d.source is s]
            if types:
                d = types[0]
                for i in range(d.begin, d.end - 2):
                    if s.tokens[i].text in ("var", "let") and s.tokens[i + 2].text == ":" and i + 3 < d.end and s.tokens[i + 3].text == "WKWebView":
                        is_member = self.owner(s, i) == d.name and not any(f.kind == "func" and f.source is s and f.begin <= i < f.end for f in self.definitions)
                        if is_member:
                            env.setdefault(s.tokens[i + 1].text, Value(type_name="WKWebView"))
        control_bodies = _control_bodies(s, a, b)
        i = a
        while i < b:
            token = s.tokens[i]
            name = token.text
            equals = i + 1
            if token.text in ("let", "var") and i + 2 < b:
                candidate = i + 2
                while candidate < b and s.tokens[candidate].text not in ("=", "{", "}", ";"):
                    if candidate > i + 2 and "\n" in s.raw[s.tokens[candidate - 1].start:s.tokens[candidate].start]:
                        break
                    candidate += 1
                if candidate < b and s.tokens[candidate].text == "=":
                    name = s.tokens[i + 1].text
                    equals = candidate
            if equals < b and s.tokens[equals].text == "=" and (equals + 1 >= b or s.tokens[equals + 1].text != "="):
                end = _end_expr(s, equals + 1, b)
                val = self.value(s, equals + 1, end, env)
                if name == "viewControllers":
                    container = env.get(_simple_receiver(s, i) or "", Value())
                    if container.type_name in ("UINavigationController", "UIKit.UINavigationController"):
                        container.args["rootViewController"] = val.args.get("0", Value()) if val.type_name == "Array" and len(val.args) == 1 else Value()
                env[name] = val
                if s.text(equals + 1, end) == "true":
                    env[name] = Value(text="true")
                assignments.add(name)
                i = end
                continue
            if token.text == "[" and i in s.pairs:
                close = s.pairs[i]
                call = _objc_call(s, i, close + 1)
                if call:
                    selector, receiver_span, arguments = call
                    receiver_name = s.text(*receiver_span).removeprefix("self.")
                    args = {label: self.value(s, x, y, env) for label, x, y in arguments}
                    receiver_value = env.get(receiver_name, Value())
                    if selector == "loadRequest" and receiver_value.type_name == "WKWebView":
                        results.append(self.loaded(args.get("loadRequest", Value()), s, i))
                    elif selector in ("loadHTMLString", "loadFileURL") and receiver_value.type_name == "WKWebView":
                        results.append(self.loaded(args.get("loadFileURL", Value()) if selector == "loadFileURL" else Value(), s, i))
                    elif selector in ("presentViewController", "pushViewController"):
                        target = args.get(selector, Value())
                        if selector == "pushViewController" and target.type_name in ("UINavigationController", "UIKit.UINavigationController"):
                            results.append(self.outcome("NOT_VERIFIABLE", "协议入口尝试 push 导航控制器本身，不能按 present 的根页面展示链判定通过。", evidence=[self.evidence(s, i)]))
                        else:
                            results += self.destination(target, s, i, seen)
                    elif selector in ("openURL", "open"):
                        receiver_code = s.text(*receiver_span)
                        if "UIApplication" in receiver_code:
                            results.append(self.outcome("FAIL", "协议通过 UIApplication 在应用外打开。", args.get(selector, Value()).text, [self.evidence(s, i)]))
                    elif receiver_name == "self":
                        methods = [d for d in self.definitions if d.kind == "func" and d.name == selector and d.owner == self.owner(s, i)]
                        if len(methods) == 1:
                            d = methods[0]
                            key = (d.source.path, d.start)
                            if key not in seen:
                                results += self.trace(d.source, d.begin, d.end, env, seen | {key}, destination=destination)
                i = close + 1
                continue
            # Skip nested declarations/closures unless invoked or explicitly used
            # as a presentation destination below.
            if token.text == "func":
                definition = next((d for d in self.definitions if d.source is s and d.start == i), None)
                if definition:
                    i = definition.end + 1
                    continue
            if token.text == "{" and i in s.pairs:
                # Ordinary branches are searched for call existence. Uncalled
                # closures stay excluded; this does not prove runtime execution.
                before = s.tokens[i - 1].text if i else ""
                if i in control_bodies or (destination and before in ("VStack", "HStack", "ZStack", "Group", "ScrollView", "NavigationStack", "NavigationView")):
                    results += self.trace(s, i + 1, s.pairs[i], env, seen, destination=destination)
                i = s.pairs[i] + 1
                continue
            if i + 1 < b and s.tokens[i + 1].text == "(" and i + 1 in s.pairs:
                close = s.pairs[i + 1]
                args = {label: self.value(s, x, y, env) for label, x, y in _args(s, i + 2, close)}
                receiver = _simple_receiver(s, i)
                if token.text == "setViewControllers":
                    container = env.get(receiver or "", Value())
                    if container.type_name in ("UINavigationController", "UIKit.UINavigationController"):
                        val = args.get("_", Value())
                        container.args["rootViewController"] = val.args.get("0", Value()) if val.type_name == "Array" and len(val.args) == 1 else Value()
                    i = close + 1
                    continue
                if token.text in ("loadHTMLString", "loadFileURL"):
                    receiver_value = env.get(receiver or "", Value())
                    if receiver_value.type_name == "WKWebView":
                        results.append(self.loaded(args.get("_", Value()) if token.text == "loadFileURL" else Value(), s, i))
                elif token.text == "load":
                    receiver_value = env.get(receiver or "", self.value(s, max(a, i - 2), i - 1, env) if receiver else Value())
                    if receiver_value.type_name == "WKWebView":
                        results.append(self.loaded(args.get("_", args.get("request", Value())), s, i))
                elif token.text in ("openURL", "open"):
                    before = s.text(max(a, i - 7), i)
                    is_external = token.text == "openURL" or "UIApplication.shared." in before or "UIApplication.sharedApplication" in before
                    if is_external:
                        value = args.get("_", args.get("url", Value()))
                        override = self._override(s, i) if token.text == "openURL" else None
                        if override:
                            branch = self._override_trace(s, override, value, env, seen, i)
                            results += branch or [self.outcome("NOT_VERIFIABLE", "自定义 openURL 处理链无法关联到 WKWebView。", value.text)]
                        else:
                            results.append(self.outcome("FAIL", "协议使用系统 openURL/UIApplication 外部打开方式。", value.text, [self.evidence(s, i)]))
                elif token.text in ("present", "pushViewController", "show"):
                    target = args.get("_", args.get("viewController", Value()))
                    if token.text == "pushViewController" and target.type_name in ("UINavigationController", "UIKit.UINavigationController"):
                        results.append(self.outcome("NOT_VERIFIABLE", "协议入口尝试 push 导航控制器本身，不能按 present 的根页面展示链判定通过。", evidence=[self.evidence(s, i)]))
                    else:
                        results += self.destination(target, s, i, seen)
                elif token.text == "SFSafariViewController":
                    # Construction only becomes a violation when presented; an
                    # unrelated unused Safari variable does not affect the flow.
                    pass
                elif destination and token.text[:1].isupper() and token.text not in ("URL", "URLRequest", "WKWebView"):
                    # Inside a SwiftUI destination, child view construction is
                    # itself its declarative presentation.
                    if not (i > a and s.tokens[i - 1].text == "="):
                        results += self.destination(Value(type_name=token.text, args=args), s, i, seen)
                elif token.text not in ("URL", "URLRequest", "WKWebView"):
                    owner = self.owner(s, i)
                    methods = [d for d in self.definitions if d.kind == "func" and d.name == token.text
                               and (d.owner == owner if receiver in (None, "self") else d.owner == receiver)]
                    if not methods and receiver is None:
                        methods = [d for d in self.definitions if d.kind == "func" and d.name == token.text and d.owner is None]
                    if len(methods) == 1:
                        d = methods[0]
                        key = (d.source.path, d.start)
                        if key not in seen:
                            bound = {**env, **self._bind(d, args)}
                            results += self.trace(d.source, d.begin, d.end, bound, seen | {key}, destination=destination)
                i = close + 1
                continue
            i += 1
        if assignments:
            for name, x, y, after, end in self._modifiers(s, modifier_index if modifier_index is not None else a):
                if name not in ("sheet", "fullScreenCover") or after >= end or s.tokens[after].text != "{":
                    continue
                arguments = _args(s, x, y)
                state = next(((label, s.text(va, vb).lstrip("$")) for label, va, vb in arguments if label in ("isPresented", "item")), None)
                if state and state[1] in assignments and (state[0] == "item" or env[state[1]].text == "true"):
                    results += self.trace(s, after + 1, end - 1, env, seen, destination=True)
        return results

    def _entry_result(self, kind, s, index, results, fallback_url=None):
        self.entry_locations.add((s.path, index))
        evidence = [self.evidence(s, index)]
        for result in list(results):
            for container in result.get("_containers", []):
                root = container.args.get("rootViewController", Value())
                if root.type_name in ("SFSafariViewController", "SafariView", "SafariViewController"):
                    results.append(self.outcome("FAIL", "协议关联导航控制器被明确切换到 Safari 页面。", root.args.get("url", Value()).text, evidence))
        if not results:
            result = self.outcome("NOT_VERIFIABLE", "已发现协议操作入口，但尚未关联到 WKWebView 加载调用。", fallback_url, evidence)
        else:
            order = {"FAIL": 0, "NOT_VERIFIABLE": 1, "PASS": 2}
            result = dict(min(results, key=lambda r: order[r["status"]]))
            result["evidence"] = evidence + [e for r in results for e in r["evidence"] if e not in evidence]
            urls = {r["url"] for r in results if r["url"] is not None}
            if len(urls) > 1 and result["status"] == "PASS":
                result["url"] = None  # URL details are optional; load-call presence is the rule.
        if self.incomplete and result["status"] == "PASS":
            result.update(status="NOT_VERIFIABLE", actual="源码读取或词法结构不完整，不能确认协议加载链完整。",
                          manual_check="补齐源码或修复读取问题后重新检查协议入口。")
        result.pop("_protocol_kind", None)
        result.pop("_containers", None)
        self.entries.append({"kind": kind, **result})

    def run(self):
        for s in self.sources:
            for i, token in enumerate(s.tokens):
                if token.text not in ("Button", "NavigationLink", "Link", "UIAction"):
                    continue
                args, closures, end = [], [], i + 1
                if end < len(s.tokens) and s.tokens[end].text == "(" and end in s.pairs:
                    close = s.pairs[end]
                    args = _args(s, end + 1, close)
                    end = close + 1
                if end < len(s.tokens) and s.tokens[end].text == "{" and end in s.pairs:
                    closures.append(("_", end + 1, s.pairs[end]))
                    end = s.pairs[end] + 1
                if end + 2 < len(s.tokens) and s.tokens[end].text == "label" and s.tokens[end + 1].text == ":" and s.tokens[end + 2].text == "{" and end + 2 in s.pairs:
                    closures.append(("label", end + 3, s.pairs[end + 2]))
                kind, support = None, False
                for label, a, b in args:
                    if label in ("_", "title"):
                        kind = self.expression_kind(s, a, b)
                        support |= (self.value(s, a, b).text or "").strip().lower() == "support"
                        if kind:
                            break
                if kind is None:
                    labels = [c for c in closures if c[0] == "label"]
                    if not labels and any(label in ("action", "destination") for label, _, _ in args):
                        labels = closures
                    for _, a, b in labels:
                        for j in range(a, b):
                            if s.tokens[j].literal is not None and j >= 2 and s.tokens[j - 2].text in ("Text", "Label"):
                                kind = self.label_kind(s.tokens[j].literal)
                                support |= s.tokens[j].literal.strip().lower() == "support"
                                if kind:
                                    break
                if kind is None and not support:
                    continue
                results, fallback_url = [], None
                if token.text == "Link":
                    destination_arg = next(((a, b) for label, a, b in args if label == "destination"), None)
                    value = self.value(s, *destination_arg) if destination_arg else Value()
                    fallback_url = value.text
                    override = self._override(s, i)
                    if override:
                        results = self._override_trace(s, override, value, {}, set(), i)
                    else:
                        results = [self.outcome("FAIL", "SwiftUI Link 使用默认系统打开方式，未关联 WKWebView。", value.text)]
                else:
                    action_args = [(a, b) for label, a, b in args if label in ("action", "destination", "handler")]
                    action_closures = [(a, b) for label, a, b in closures if label == "_"]
                    if action_args:
                        actions = action_args
                    else:
                        actions = action_closures
                    for a, b in actions:
                        if a < b and s.tokens[a].text == "{" and s.pairs.get(a) == b - 1:
                            a, b = a + 1, b - 1
                        results += self.trace(s, a, b, destination=token.text == "NavigationLink")
                        if b == a + 1:
                            methods = [d for d in self.definitions if d.kind == "func" and d.name == s.tokens[a].text and d.owner == self.owner(s, i)]
                            if len(methods) == 1:
                                d = methods[0]
                                results += self.trace(d.source, d.begin, d.end)
                if kind is None:
                    inferred = {r.get("_protocol_kind") for r in results if r.get("_protocol_kind")}
                    if inferred != {"terms"}:
                        continue
                    kind = "terms"
                self._entry_result(kind, s, i, results, fallback_url)
            self._uikit(s)
            self._table_entries(s)
            self._factory_entries(s)
            self._fallback_candidates(s)
        for kind, title in (("privacy", "隐私协议"), ("terms", "用户协议")):
            if not any(entry["kind"] == kind for entry in self.entries):
                self.entries.append({"kind": kind, "missing": not self.incomplete, **self.outcome(
                    "NOT_VERIFIABLE" if self.incomplete else "FAIL",
                    f"源码扫描不完整，无法确认{title}入口。" if self.incomplete else f"完整源码扫描未发现可识别的{title}操作入口。")})
        return self.entries

    def _uikit(self, s):
        for i in range(2, len(s.tokens) - 2):
            if s.tokens[i].text != "setTitle" or s.tokens[i - 1].text != "." or s.tokens[i + 1].text != "(" or i + 1 not in s.pairs:
                continue
            args = _args(s, i + 2, s.pairs[i + 1])
            if not args:
                continue
            title = self.value(s, args[0][1], args[0][2]).text
            kind = self.label_kind(title) if title else None
            support = (title or "").strip().lower() == "support"
            if not kind and not support:
                continue
            receiver = s.tokens[i - 2].text
            owner = self.owner(s, i)
            methods = []
            for j in range(2, len(s.tokens) - 2):
                if s.tokens[j].text != "addTarget" or s.tokens[j - 2].text != receiver or s.tokens[j - 1].text != "." or self.owner(s, j) != owner:
                    continue
                if self._binding_key(s, i, receiver) != self._binding_key(s, j, receiver):
                    continue
                if j + 1 not in s.pairs:
                    continue
                call = s.text(j + 2, s.pairs[j + 1])
                match = re.search(r"action:#selector\((?:self\.)?(\w+)", call)
                if match:
                    methods += [d for d in self.definitions if d.kind == "func" and d.name == match[1] and d.owner == owner]
            results = []
            if len(methods) == 1:
                d = methods[0]
                results = self.trace(d.source, d.begin, d.end)
            if kind is None:
                inferred = {r.get("_protocol_kind") for r in results if r.get("_protocol_kind")}
                if inferred != {"terms"}:
                    continue
                kind = "terms"
            self._entry_result(kind, s, i, results)
        for i, token in enumerate(s.tokens):
            if token.text != "[" or i not in s.pairs:
                continue
            call = _objc_call(s, i, s.pairs[i] + 1)
            if not call or call[0] != "setTitle" or not call[2]:
                continue
            title = self.value(s, call[2][0][1], call[2][0][2]).text
            kind = self.label_kind(title) if title else None
            support = (title or "").strip().lower() == "support"
            if not kind and not support:
                continue
            receiver = s.text(*call[1])
            owner = self.owner(s, i)
            actions = []
            for j, candidate in enumerate(s.tokens):
                if candidate.text != "[" or j not in s.pairs or self.owner(s, j) != owner:
                    continue
                target = _objc_call(s, j, s.pairs[j] + 1)
                if target and target[0] == "addTarget" and s.text(*target[1]) == receiver:
                    for label, a, b in target[2]:
                        if label == "action":
                            match = re.fullmatch(r"@selector\((\w+)\)", s.text(a, b))
                            if match:
                                actions += [d for d in self.definitions if d.kind == "func" and d.name == match[1] and d.owner == owner]
            results = []
            if len(actions) == 1:
                d = actions[0]
                results = self.trace(d.source, d.begin, d.end)
            if kind is None:
                inferred = {r.get("_protocol_kind") for r in results if r.get("_protocol_kind")}
                if inferred != {"terms"}:
                    continue
                kind = "terms"
            self._entry_result(kind, s, i, results)

    def _binding_key(self, s, index, name):
        scopes = s.ancestors(index)
        visible = [c for c in self.constants if c[0] is s and c[1] == name and c[3] < index
                   and all(scope in scopes for scope in s.ancestors(c[3]))]
        if visible:
            chosen = max(visible, key=lambda c: (len(s.ancestors(c[3])), c[3]))
            return ("declaration", chosen[3])
        parameters = [d for d in self.definitions if d.source is s and d.kind == "func" and d.begin <= index < d.end
                      and any(param == name for _, param, _ in d.params)]
        if parameters:
            return ("parameter", parameters[0].start, name)
        return ("property", self.owner(s, index), name)

    def _table_context(self, s, index, method, parameter):
        context = {}
        for expression, cases in _switches(s, method.begin, method.end):
            selector = next((field for field in ("section", "row") if parameter + "." + field in expression), None)
            if selector is None:
                continue
            for a, b, begin, end in cases:
                if begin <= index < end:
                    value = s.text(a, b)
                    discriminator = expression.replace(parameter + ".", "$index.")
                    context[selector] = (discriminator, "==" + value if value.isdigit() else value)
        for j in range(method.begin, method.end):
            if s.tokens[j].text != "if":
                continue
            opening = next((k for k in range(j + 1, method.end) if s.tokens[k].text == "{"), None)
            if opening not in s.pairs:
                continue
            predicate = _row_predicate(s.text(j + 1, opening), parameter)
            if predicate is None:
                if parameter + ".row" in s.text(j + 1, opening) and opening < index < s.pairs[opening]:
                    context["unresolved_row"] = True
                continue
            close = s.pairs[opening]
            if opening < index < close:
                context[predicate[0]] = predicate[1]
            elif close + 2 < method.end and s.text(close + 1, close + 3) == "else{" and close + 2 in s.pairs and close + 2 < index < s.pairs[close + 2]:
                context[predicate[0]] = ("!=" if predicate[1].startswith("==") else "==") + predicate[1][2:]
        return context

    def _table_entries(self, s):
        """Associate visible table labels with the same section/row click branch."""
        cells = [d for d in self.definitions if d.source is s and d.kind == "func"
                 and any(label == "cellForRowAt" for label, _, _ in d.params)]
        for cell in cells:
            selectors = [d for d in self.definitions if d.kind == "func" and d.owner == cell.owner
                         and any(label == "didSelectRowAt" for label, _, _ in d.params)]
            parameter = next((name for _, name, typ in cell.params if typ == "IndexPath"), None)
            if parameter is None:
                continue
            for j in _execution_indices(s, cell.begin, cell.end - 2):
                if s.tokens[j].text != "text" or s.tokens[j + 1].text != "=" or "textLabel" not in s.text(max(cell.begin, j - 5), j):
                    continue
                end = _end_expr(s, j + 2, cell.end)
                question = next((k for k in range(j + 2, end) if s.tokens[k].text == "?"), None)
                colon = next((k for k in range((question + 1) if question else end, end) if s.tokens[k].text == ":"), None)
                predicate = _row_predicate(s.text(j + 2, question), parameter) if question is not None else None
                for label_index in range(j + 2, end):
                    literal = s.tokens[label_index].literal
                    kind = self.label_kind(literal) if literal is not None else None
                    if kind is None:
                        continue
                    context = self._table_context(s, label_index, cell, parameter)
                    if predicate and colon:
                        context[predicate[0]] = predicate[1] if label_index < colon else ("!=" if predicate[1].startswith("==") else "==") + predicate[1][2:]
                    elif question is not None:
                        context["unresolved_row"] = True
                    results = []
                    if len(selectors) == 1 and "unresolved_row" not in context:
                        selected = selectors[0]
                        ss = selected.source
                        selected_parameter = next((name for _, name, typ in selected.params if typ == "IndexPath"), None)
                        if selected_parameter:
                            for k in _execution_indices(ss, selected.begin, selected.end - 1):
                                if ss.tokens[k].text not in ("present", "pushViewController", "show", "openURL", "open") or ss.tokens[k + 1].text != "(" or k + 1 not in ss.pairs:
                                    continue
                                if self._table_context(ss, k, selected, selected_parameter) != context:
                                    continue
                                # Receiver prefixes preserve UIApplication/openURL
                                # semantics when tracing this associated call only.
                                start = k
                                while start > selected.begin and ss.tokens[start - 1].text in (".", "?", "!"):
                                    start -= 1
                                    if start > selected.begin and ss.tokens[start - 1].text not in (".", "?", "!"):
                                        start -= 1
                                results += self.trace(ss, start, ss.pairs[k + 1] + 1)
                    self._entry_result(kind, s, label_index, results)

    def _factory_entries(self, s):
        """Resolve called UIButton factories and the corresponding identifier case.

        The returned button must bind its title, identifier and target action;
        an invocation must be used in an addSubview/addArrangedSubview call.
        Neither an unused factory declaration nor an adjacent switch case counts.
        """
        factories = [d for d in self.definitions if d.kind == "func" and "->UIButton" in d.source.text(d.start, d.begin)]
        for factory in factories:
            fs = factory.source
            returns = [fs.tokens[k + 1].text for k in range(factory.begin, factory.end - 1) if fs.tokens[k].text == "return"]
            if len(returns) != 1:
                continue
            button = returns[0]
            title_arg = identifier_arg = action = None
            for k in range(factory.begin, factory.end - 2):
                if _simple_receiver(fs, k) != button:
                    continue
                if fs.tokens[k].text == "setTitle" and fs.tokens[k + 1].text == "(" and k + 1 in fs.pairs:
                    args = _args(fs, k + 2, fs.pairs[k + 1])
                    if args:
                        title_arg = args[0][1:]
                elif fs.tokens[k].text == "accessibilityIdentifier" and fs.tokens[k + 1].text == "=":
                    identifier_arg = (k + 2, _end_expr(fs, k + 2, factory.end))
                elif fs.tokens[k].text == "addTarget" and fs.tokens[k + 1].text == "(" and k + 1 in fs.pairs:
                    call = fs.text(k + 2, fs.pairs[k + 1])
                    match = re.search(r"^self,action:#selector\((?:self\.)?(\w+)", call)
                    if match:
                        action = match[1]
            if not all((title_arg, identifier_arg, action)):
                continue
            handlers = [d for d in self.definitions if d.kind == "func" and d.name == action and d.owner == factory.owner]
            for i in range(len(s.tokens) - 1):
                if s.tokens[i].text != factory.name or s.tokens[i + 1].text != "(" or i + 1 not in s.pairs or self.owner(s, i) != factory.owner:
                    continue
                if s is fs and i == factory.start + 1:
                    continue
                visible_use = any(opening < i < close and opening > 0 and s.tokens[opening].text == "(" and s.tokens[opening - 1].text in ("addSubview", "addArrangedSubview") for opening, close in s.pairs.items() if opening < close)
                if not visible_use:
                    continue
                arguments = {label: self.value(s, a, b) for label, a, b in _args(s, i + 2, s.pairs[i + 1])}
                bound = self._bind(factory, arguments)
                label = self.value(fs, *title_arg, bound).text
                kind = self.label_kind(label) if label else None
                if kind is None:
                    continue
                identifier = self.value(fs, *identifier_arg, bound).text
                results = []
                if identifier is not None and len(handlers) == 1:
                    handler = handlers[0]
                    hs = handler.source
                    senders = [name for _, name, typ in handler.params if typ == "UIButton"]
                    if len(senders) == 1:
                        for expression, cases in _switches(hs, handler.begin, handler.end):
                            if expression != senders[0] + ".accessibilityIdentifier":
                                continue
                            matching = [(begin, end) for a, b, begin, end in cases if self.value(hs, a, b).text == identifier]
                            if len(matching) == 1:
                                results += self.trace(hs, *matching[0])
                self._entry_result(kind, s, i, results)

    def _fallback_candidates(self, s):
        """Recognise obvious custom control candidates without claiming routing.

        A title alone is prose. A named UI wrapper plus action/destination, or a
        Text with an actual tap modifier, is enough to retain an unknown entry.
        """
        for i, token in enumerate(s.tokens):
            if (s.path, i) in self.entry_locations or i + 1 >= len(s.tokens) or s.tokens[i + 1].text != "(" or i + 1 not in s.pairs:
                continue
            close = s.pairs[i + 1]
            args = _args(s, i + 2, close)
            labels = [(a, b) for name, a, b in args if name in ("title", "label", "text", "_")]
            kind = next((kind for a, b in labels if (kind := self.expression_kind(s, a, b))), None)
            known_control = token.text in ("Button", "NavigationLink", "Link", "UIAction")
            if kind is None and known_control:
                # Unresolved localized/computed labels can still identify which
                # protocol requires manual tracing; they are never passed.
                for a, b in labels:
                    name = s.text(a, b)
                    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name):
                        if re.search("privacy", name, re.I):
                            kind = "privacy"
                        elif re.search(r"terms|userAgreement", name, re.I):
                            kind = "terms"
            if kind is None:
                continue
            has_action = any(name in ("action", "onTap", "onPress", "destination", "handler") for name, _, _ in args)
            custom_control = token.text[:1].isupper() and bool(re.search(r"Row|Cell|Button|Link|Item|Tile|Control", token.text))
            tap = token.text in ("Text", "Label") and close + 2 < len(s.tokens) and s.tokens[close + 1].text == "." and s.tokens[close + 2].text == "onTapGesture"
            if known_control or (custom_control and has_action) or tap:
                self._entry_result(kind, s, i, [])


def analyze_legal_links(root: Path, source_texts: Mapping[Path, str], all_texts: Mapping[Path, str], *, scan_incomplete: bool = False) -> list[dict]:
    """Return per-entry privacy/terms evidence for associated WK loading calls.

    Recognises literal/unique Swift constants, enum raw values, direct SwiftUI
    navigation, scoped Link handlers, state-driven sheets, ordinary representable
    wrappers, UIKit target/actions, matched table sections/rows, and returned
    UIButton factories with identifier dispatch. Explicit Info.plist lookups and
    .strings/.xcstrings labels are resolved only when unique. Custom controls are
    retained as unknown candidates. Dynamic URL values do not block a found load
    call. Runtime config decoding, macros and general control-flow analysis are
    outside this check; document prose alone is never a user-interface entry.
    """
    return Analyzer(root, source_texts, all_texts, scan_incomplete).run()
