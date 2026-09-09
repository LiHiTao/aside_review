"""Conservative source association for in-app legal-document navigation.

This is a bounded Swift/Objective-C analyser, not a compiler. Balanced lexical
scopes and unique argument/constant bindings are required for a positive result;
unsupported routing is reported as NOT_VERIFIABLE rather than inferred from a
nearby WKWebView name. No code is executed and no network request is made here.
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
    if re.fullmatch(r"(?:terms(?: of (?:service|use))?|user agreement|user terms|terms (?:and|&) conditions|eula|用户(?:协议|条款)|使用(?:协议|条款)|用戶(?:協議|條款))", label):
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
                if token.text in ("func", "init") and paren < len(s.tokens) and s.tokens[paren].text == "(" and paren in s.pairs:
                    close = s.pairs[paren]
                    j = close + 1
                    while j < len(s.tokens) and s.tokens[j].text not in ("{", "}", ";", "func"):
                        j += 1
                    if j not in s.pairs:
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
                "manual_check": "在源码或 App 中核对该协议入口实际传递的 URL 与 WKWebView 加载链。" if status == "NOT_VERIFIABLE" else None}

    def loaded(self, value, s, index, expected=None):
        evidence = [self.evidence(s, index)]
        if value.text is None:
            return self.outcome("NOT_VERIFIABLE", "WKWebView 已关联，但 URL 是动态表达式或绑定无法唯一解析。", evidence=evidence)
        if expected and expected.text is not None and value.text != expected.text:
            return self.outcome("FAIL", "协议入口传入的 URL 未被实际加载，WKWebView 加载了另一个地址。", value.text, evidence)
        if expected is not None and expected.text is None:
            return self.outcome("NOT_VERIFIABLE", "入口 URL 为动态参数，不能确认封装实际加载地址与该参数一致。", evidence=evidence)
        if value.text.startswith("file:"):
            return self.outcome("FAIL", "协议使用本地文件，未通过 WKWebView 加载网页 URL。", value.text, evidence)
        return self.outcome("PASS", "协议入口、URL 参数和应用内 WKWebView 加载链有明确静态关联。", value.text, evidence)

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
        initializers = [f for f in self.definitions if f.kind == "func" and f.source is d.source and f.owner == d.name and f.name == "init"]
        if initializers:
            matching = [f for f in initializers if set(value.args) <= {label for label, _, _ in f.params}]
            if len(matching) != 1:
                return [self.outcome("NOT_VERIFIABLE", "页面封装构造器无法唯一匹配，不能确认 URL 属性绑定。", evidence=[self.evidence(s, index)])]
            constructor = matching[0]
            arguments = self._bind(constructor, value.args)
            properties = {}
            cs = constructor.source
            for j in range(constructor.begin, constructor.end - 4):
                if cs.tokens[j].text in ("if", "switch", "guard", "for", "while"):
                    return [self.outcome("NOT_VERIFIABLE", "页面构造器包含条件或动态属性赋值，不能唯一绑定协议 URL。", evidence=[self.evidence(cs, j)])]
                if cs.text(j, j + 2) == "self." and cs.tokens[j + 3].text == "=":
                    properties[cs.tokens[j + 2].text] = self.value(cs, j + 4, _end_expr(cs, j + 4, constructor.end), arguments)
        methods = [f for f in self.definitions if f.kind == "func" and f.source is d.source and f.owner == d.name
                   and f.name in ("makeUIView", "updateUIView", "makeUIViewController", "updateUIViewController", "viewDidLoad", "loadView")]
        url_args = [v for k, v in value.args.items() if re.search(r"url|link|address", k, re.I)]
        expected = url_args[0] if len(url_args) == 1 else None
        results = []
        for method in methods:
            env = self._bind(method, properties)
            mounted = set()
            ms = method.source
            if method.name == "updateUIView":
                mounted = {name for _, name, type_name in method.params if type_name.startswith("WKWebView")}
            elif method.name == "makeUIView":
                returns = [j for j in range(method.begin, method.end) if ms.tokens[j].text == "return"]
                if len(returns) == 1:
                    j = returns[0] + 1
                    k = _end_expr(ms, j, method.end)
                    if k == j + 1:
                        mounted.add(ms.tokens[j].text)
            elif method.name in ("viewDidLoad", "loadView"):
                for j in range(method.begin, method.end - 3):
                    if ms.tokens[j].text == "addSubview" and ms.tokens[j + 1].text == "(" and ms.pairs.get(j + 1) == j + 3:
                        start = j - 2
                        while start >= method.begin + 2 and ms.tokens[start - 1].text == ".":
                            start -= 2
                        if ms.text(start, j - 1) in ("view", "self.view"):
                            mounted.add(ms.tokens[j + 2].text)
                    if ms.tokens[j].text == "view" and ms.tokens[j + 1].text == "=":
                        if j == method.begin or ms.tokens[j - 1].text != "." or (j >= method.begin + 2 and ms.tokens[j - 2].text == "self"):
                            mounted.add(ms.tokens[j + 2].text)
                    if ms.tokens[j].text == "[" and j in ms.pairs:
                        call = _objc_call(ms, j, ms.pairs[j] + 1)
                        if call and call[0] == "addSubview" and call[2] and ms.text(*call[1]) in ("self.view", "[selfview]"):
                            mounted.add(ms.text(call[2][0][1], call[2][0][2]).removeprefix("self."))
            results += self.trace(method.source, method.begin, method.end, env, seen,
                                  expected=expected, destination=True, mounted=mounted)
        # SwiftUI view composition (NavigationLink -> wrapper View -> representable).
        if not methods:
            for j in range(d.begin, d.end - 2):
                if d.source.tokens[j].text == "var" and d.source.tokens[j + 1].text == "body":
                    brace = next((k for k in range(j + 2, d.end) if d.source.tokens[k].text == "{"), None)
                    if brace in d.source.pairs:
                        results += self.trace(d.source, brace + 1, d.source.pairs[brace], properties, seen,
                                              expected=expected, destination=True)
        return results

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
        if not re.search(r"return\.handled\b", code):
            return []
        return self.trace(s, body, close, {**env, param: value}, seen, modifier_index=entry_index)

    def trace(self, s, a, b, env=None, seen=None, expected=None, destination=False, mounted=None, modifier_index=None):
        env = dict(env or {})
        seen = seen or set()
        results = []
        mounted = set(mounted or ())
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
                        env.setdefault(s.tokens[i + 1].text, Value(type_name="WKWebView"))
        i = a
        while i < b:
            token = s.tokens[i]
            if token.text in ("if", "guard", "switch", "for", "while", "catch"):
                results.append(self.outcome("NOT_VERIFIABLE", "协议处理链包含条件、循环或异常分支，当前静态模式不能确认所有实际打开路径。", evidence=[self.evidence(s, i)]))
            if i + 1 < b and s.tokens[i + 1].text == "=" and (i + 2 >= b or s.tokens[i + 2].text != "="):
                name = token.text
                end = _end_expr(s, i + 2, b)
                env[name] = self.value(s, i + 2, end, env)
                if s.text(i + 2, end) == "true":
                    env[name] = Value(text="true")
                if name in assignments and name in mounted:
                    results.append(self.outcome("NOT_VERIFIABLE", "同一 WebView 引用被多次赋值，无法确认已展示实例与加载实例相同。", evidence=[self.evidence(s, i)]))
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
                    if selector == "loadRequest" and receiver_value.type_name == "WKWebView" and receiver_name in mounted:
                        results.append(self.loaded(args.get("loadRequest", Value()), s, i, expected))
                    elif selector in ("loadHTMLString", "loadFileURL") and receiver_value.type_name == "WKWebView" and receiver_name in mounted:
                        results.append(self.outcome("FAIL", "协议通过 WKWebView 加载本地 HTML 或文件。", "", [self.evidence(s, i)]))
                    elif selector in ("presentViewController", "pushViewController"):
                        results += self.destination(args.get(selector, Value()), s, i, seen)
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
                                results += self.trace(d.source, d.begin, d.end, env, seen | {key}, expected, destination, mounted)
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
                # Closures and conditional branches are not guaranteed execution.
                # Known declarative container bodies may compose child views.
                before = s.tokens[i - 1].text if i else ""
                if destination and before in ("VStack", "HStack", "ZStack", "Group", "ScrollView", "NavigationStack", "NavigationView"):
                    results += self.trace(s, i + 1, s.pairs[i], env, seen, expected, True, mounted)
                i = s.pairs[i] + 1
                continue
            if i + 1 < b and s.tokens[i + 1].text == "(" and i + 1 in s.pairs:
                close = s.pairs[i + 1]
                args = {label: self.value(s, x, y, env) for label, x, y in _args(s, i + 2, close)}
                receiver = s.tokens[i - 2].text if i >= 2 and s.tokens[i - 1].text == "." else None
                if token.text in ("loadHTMLString", "loadFileURL"):
                    receiver_value = env.get(receiver or "", Value())
                    if receiver_value.type_name == "WKWebView":
                        if receiver in mounted:
                            local_url = args.get("_", Value()).text if token.text == "loadFileURL" else ""
                            results.append(self.outcome("FAIL", "协议通过 WKWebView 加载本地 HTML 或文件，未加载网页 URL。", local_url or "", [self.evidence(s, i)]))
                elif token.text == "load":
                    receiver_value = env.get(receiver or "", self.value(s, max(a, i - 2), i - 1, env) if receiver else Value())
                    if receiver_value.type_name == "WKWebView" and receiver in mounted:
                        results.append(self.loaded(args.get("_", args.get("request", Value())), s, i, expected))
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
                            helper_mounted = mounted | {name for name, val in bound.items()
                                                        if any(val is env.get(mount) for mount in mounted)}
                            results += self.trace(d.source, d.begin, d.end, bound, seen | {key}, expected, destination, helper_mounted)
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
                    results += self.trace(s, after + 1, end - 1, env, seen, expected, destination=True)
        return results

    def _entry_result(self, kind, s, index, results, fallback_url=None):
        self.entry_locations.add((s.path, index))
        evidence = [self.evidence(s, index)]
        if not results:
            result = self.outcome("NOT_VERIFIABLE", "已发现协议操作入口，但当前支持的静态模式无法关联完整的页面、参数与 WKWebView 加载链。", fallback_url, evidence)
        else:
            order = {"FAIL": 0, "NOT_VERIFIABLE": 1, "PASS": 2}
            result = dict(min(results, key=lambda r: order[r["status"]]))
            result["evidence"] = evidence + [e for r in results for e in r["evidence"] if e not in evidence]
            urls = {r["url"] for r in results if r["url"] is not None}
            if len(urls) > 1 and result["status"] == "PASS":
                result = self.outcome("NOT_VERIFIABLE", "同一协议入口关联多个不同加载地址，无法唯一确定实际 URL。", evidence=result["evidence"])
        if self.incomplete and result["status"] == "PASS":
            result.update(status="NOT_VERIFIABLE", actual="源码读取或词法结构不完整，不能确认协议加载链完整。",
                          manual_check="补齐源码或修复读取问题后重新检查协议入口。")
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
                kind = None
                for label, a, b in args:
                    if label in ("_", "title"):
                        kind = self.expression_kind(s, a, b)
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
                                if kind:
                                    break
                if kind is None:
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
                self._entry_result(kind, s, i, results, fallback_url)
            self._uikit(s)
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
            if not kind:
                continue
            receiver = s.tokens[i - 2].text
            owner = self.owner(s, i)
            methods = []
            for j in range(2, len(s.tokens) - 2):
                if s.tokens[j].text != "addTarget" or s.tokens[j - 2].text != receiver or s.tokens[j - 1].text != "." or self.owner(s, j) != owner:
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
            self._entry_result(kind, s, i, results)
        for i, token in enumerate(s.tokens):
            if token.text != "[" or i not in s.pairs:
                continue
            call = _objc_call(s, i, s.pairs[i] + 1)
            if not call or call[0] != "setTitle" or not call[2]:
                continue
            title = self.value(s, call[2][0][1], call[2][0][2]).text
            kind = self.label_kind(title) if title else None
            if not kind:
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
    """Return per-entry privacy/terms evidence; unsupported bindings stay unknown.

    Recognises literal/unique Swift constants, enum raw values, direct SwiftUI
    navigation, scoped Link handlers, state-driven sheets, ordinary representable
    wrappers and basic UIKit target/actions. Explicit Info.plist key lookups and
    .strings/.xcstrings labels are resolved only when unique. Custom controls are
    retained as unknown candidates. Runtime config decoding, arbitrary
    computed enum switches, macros and general control-flow analysis are outside
    the supported proof boundary. all_texts is accepted for a stable integration
    interface; document prose alone is never a user-interface entry.
    """
    return Analyzer(root, source_texts, all_texts, scan_incomplete).run()
