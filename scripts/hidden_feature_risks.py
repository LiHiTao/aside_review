"""Read-only indicators of concealed behavior and misleading demo presentation.

These bounded source associations are review leads, never an Apple decision.
No source, remote page or application is executed by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

try:
    from .legal_links import Analyzer
    from .restore_detection import _lex, _mask
except ImportError:
    from legal_links import Analyzer
    from restore_detection import _lex, _mask

TITLE = "5.6 隐藏功能与展示真实性风险"
LIMIT = "仅检查已覆盖的静态关联线索，不证明线上包存在隐藏功能或已违反 Apple 规则。"
LABELS = {"routing": "隐藏业务切换", "seed": "演示内容写入业务存储",
          "fallback": "素材替代用户内容", "progress": "模拟业务进度", "local_success": "本地结果作为成功返回"}
SEED = re.compile(r"demo|starter|mock|seed|sample", re.I)
STORE = re.compile(r"\b(?:\w*(?:upsert|insert|save|persist|commit|publishLocal)\w*)\s*\(|\.write\s*\(", re.I)
CONSTRUCTOR = re.compile(r"\b[A-Z]\w*(?:Record|Card|Piece|Post|Ticket|Shell|Pin|Entry)(?:\s*\.\s*\w+)?\s*\(")
PLACEHOLDER = re.compile(r"placeholder|empty|blank|default.?avatar|icon|logo|symbol", re.I)


def _argument(code, opening):
    depth, end = 1, opening + 1
    while end < len(code) and depth:
        depth += (code[end] == "(") - (code[end] == ")")
        end += 1
    return code[opening + 1:end - 1] if not depth else ""


def _constructed_bindings(code):
    values = []
    for construction in CONSTRUCTOR.finditer(code):
        prefix = code[:construction.start()]
        binding = re.search(r"\b(?:let|var)\s+(\w+)\s*(?::[^=\n]+)?=\s*$", prefix)
        append = re.search(r"\b(\w+)\s*\.\s*append\s*\(\s*$", prefix)
        if binding or append:
            values.append(((binding or append).group(1), construction.start()))
    return values


@dataclass
class Unit:
    path: Path
    owner: str | None
    name: str
    start: int
    end: int
    clean: str
    code: str


def _release_debug(all_texts):
    """Read explicit Release build settings; absent/unparseable is unknown."""
    flags = []
    for path, raw in all_texts.items():
        if path.name != "project.pbxproj":
            continue
        # Build configuration objects contain a nested buildSettings block.
        for match in re.finditer(r"\bname\s*=\s*\"?Release\"?\s*;", raw):
            stack = []
            prefix = _lex(raw[:match.start()])[1]
            for pos, char in enumerate(prefix):
                if char == "{": stack.append(pos)
                elif char == "}" and stack: stack.pop()
            if not stack:
                continue
            section = raw[stack[-1]:match.end()]
            if "buildSettings" not in section:
                continue
            conditions = re.findall(r"(?:SWIFT_ACTIVE_COMPILATION_CONDITIONS|GCC_PREPROCESSOR_DEFINITIONS)\s*=([^;]+);", section)
            has_debug = any(re.search(r"\bDEBUG(?:\s*=\s*1)?\b", value) for value in conditions)
            unresolved = "baseConfigurationReference" in section or any("$(" in value for value in conditions)
            flags.append(True if has_debug else None if unresolved else False)
    if not flags:
        return None
    # Any Release configuration can include these branches; mixed settings are a lead.
    return True if True in flags else None if None in flags else False


def _condition(expr, debug):
    expr = expr.strip()
    if expr in ("DEBUG", "defined(DEBUG)", "defined DEBUG"):
        return debug
    if expr in ("!DEBUG", "!defined(DEBUG)"):
        return None if debug is None else not debug
    if expr in ("0", "false"): return False
    if expr in ("1", "true"): return True
    return None


def _release_source(raw, debug):
    """Mask provably inactive branches and retain unknown branch offset ranges."""
    clean = _lex(raw)[0]
    result, unknown, stack, offset = [], [], [], 0
    current = True
    def conjunction(a, b):
        return False if a is False or b is False else None if a is None or b is None else True
    for line in clean.splitlines(keepends=True):
        directive = re.match(r"\s*#\s*(if|ifdef|ifndef|elseif|elif|else|endif)\b(.*)", line)
        if directive:
            kind, expr = directive.groups()
            if kind in ("if", "ifdef", "ifndef"):
                value = _condition(expr, debug)
                if kind == "ifndef": value = None if value is None else not value
                stack.append((current, value))
                current = conjunction(current, value)
            elif stack and kind in ("elseif", "elif"):
                parent, taken = stack[-1]
                value = _condition(expr, debug)
                available = None if taken is None else not taken
                current = conjunction(parent, conjunction(available, value))
                stack[-1] = (parent, True if taken is True or value is True else None if taken is None or value is None else False)
            elif stack and kind == "else":
                parent, taken = stack[-1]
                current = conjunction(parent, None if taken is None else not taken)
                stack[-1] = (parent, True)
            elif stack and kind == "endif":
                current = stack.pop()[0]
            result.append(_mask(line))
        else:
            result.append(_mask(line) if current is False else line)
            if current is None: unknown.append((offset, offset + len(line)))
        offset += len(line)
    return "".join(result), unknown, bool(stack)


def analyze_hidden_risks(root: Path, sources: Mapping[Path, str], all_texts: Mapping[Path, str], *, scan_incomplete=False):
    root = root.resolve()
    debug = _release_debug(all_texts)
    prepared, unknowns = {}, {}
    incomplete = scan_incomplete
    for path, raw in sources.items():
        if path.suffix.lower() not in (".swift", ".m", ".mm"):
            continue
        text, unknown, broken = _release_source(raw, debug)
        prepared[path] = text
        unknowns[path] = unknown
        incomplete |= broken
    analyzer = Analyzer(root, prepared, {}, False)
    incomplete |= analyzer.incomplete
    units = []
    for definition in analyzer.definitions:
        if definition.kind != "func": continue
        source = definition.source
        if definition.begin >= len(source.tokens) or definition.end >= len(source.tokens): continue
        start = source.tokens[definition.start].start
        end = source.tokens[definition.end].start
        clean, code, _ = _lex(prepared[source.path])
        units.append(Unit(source.path, definition.owner, definition.name, start, end, clean[start:end], code[start:end]))
    # SwiftUI computed body properties have no function declaration.
    for source in analyzer.sources:
        for i, token in enumerate(source.tokens):
            if token.text != "var" or i + 1 >= len(source.tokens) or source.tokens[i + 1].text != "body": continue
            opening = next((k for k in range(i + 2, len(source.tokens)) if source.tokens[k].text in ("{", "=")), None)
            if opening is None or source.tokens[opening].text != "{" or opening not in source.pairs: continue
            start, end = token.start, source.tokens[source.pairs[opening]].start
            clean, code, _ = _lex(prepared[source.path])
            units.append(Unit(source.path, analyzer.owner(source, i), "body", start, end, clean[start:end], code[start:end]))
    if not prepared:
        incomplete = True
    found = []
    def emit(category, unit, positions, reason, high=False, force_unknown=False, extra=()):
        locations = [(unit, position) for position in positions] + list(extra)
        uncertain = force_unknown or any(a <= u.start + position < b for u, position in locations for a, b in unknowns[u.path])
        evidence = []
        for u, position in locations:
            raw, pos = sources[u.path], u.start + position
            line = raw.count("\n", 0, pos) + 1
            item = {"path": str(u.path.relative_to(root)), "line": line,
                    "excerpt": raw.splitlines()[line - 1].strip()[:220]}
            if item not in evidence: evidence.append(item)
        entry = {"id": "REVIEW-001", "label": LABELS[category], "status": "NOT_VERIFIABLE" if uncertain else "WARN",
                 "severity": "high" if high and not uncertain else "medium", "actual": reason + ("；编译条件或调用/用途关联尚无法确认。" if uncertain else "。"),
                 "expected": "功能及展示结果对用户和审核人员透明、准确", "evidence": evidence,
                 "manual_check": "核对 Release 编译配置、实际用户界面及披露方式；本线索不等于确定违规。"}
        if not any(x["label"] == entry["label"] and x["evidence"] == evidence for x in found): found.append(entry)

    # Providers must actually construct records, not merely carry a seed-like name.
    providers = []
    for unit in units:
        context = " ".join((unit.name, unit.owner or "", unit.path.stem))
        constructor = CONSTRUCTOR.search(unit.code)
        if SEED.search(context) and constructor:
            providers.append(unit)

    for unit in units:
        code, clean = unit.code, unit.clean
        # A condition must enclose the switch sink; mere remote configuration is not a gate.
        for condition in re.finditer(r"\b(?:if|guard)\s+([^{}\n]+)\{", code):
            expression = condition.group(1)
            review = re.search(r"\b\w*(?:reviewMode|reviewing|inReview|isReview|isReviewer|auditMode)\w*\b", expression, re.I)
            business = re.search(r"\b\w*(?:showBM|hasBM)\w*\b", expression, re.I)
            remote = re.search(r"\b\w*remote\w*(?:business|social|web|feature)\w*\b", expression, re.I)
            if not (review or business or remote): continue
            opening = condition.end() - 1
            depth, end = 1, opening + 1
            while end < len(code) and depth:
                depth += (code[end] == "{") - (code[end] == "}")
                end += 1
            sink = re.search(r"\brootViewController\s*=|\b(?:show|open|switchTo|present|load)\w*(?:Business|Social|Web|Page|Controller)\w*\s*\(|\.load\s*\(|\.isHidden\s*=|\.alpha\s*=", code[opening:end], re.I)
            if sink: emit("routing", unit, [condition.start(), opening + sink.start()], "条件分支以审核/业务显隐状态控制页面或窗口行为，需确认不存在审核差异展示", bool(review), bool(remote and not (review or business)))
        if unit in providers:
            for name, position in _constructed_bindings(code):
                storage = next((call for call in STORE.finditer(code, position)
                                if re.search(r"\b" + re.escape(name) + r"\b", _argument(code, call.end() - 1))), None)
                if not storage:
                    # Explicit collection.forEach -> storage($0), rather than unrelated save().
                    loop = re.search(r"\b" + re.escape(name) + r"\s*\.\s*forEach\s*\{([^{}]*)\}", code[position:])
                    if loop and re.search(r"\$0\b", loop.group(1)):
                        call = STORE.search(loop.group(1))
                        if call: storage = (position + loop.start(1) + call.start())
                if storage is not None:
                    offset = storage if isinstance(storage, int) else storage.start()
                    emit("seed", unit, [position, offset], "预置记录构造值被传入存储操作，需复核是否进入正式列表、统计及是否明确标为示例")
                    break
        # A named provider result must flow into an assignment plus a persistence call.
        for provider in providers:
            if provider is unit: continue
            qualified = re.escape(provider.owner or "") + r"\s*\.\s*" if provider.owner else ""
            call = re.search(r"\b(?:let|var)\s+(\w+)\s*=\s*" + qualified + re.escape(provider.name) + r"\s*\(", code)
            if not call: continue
            assignment = re.search(r"\b(\w+)\s*=\s*" + re.escape(call.group(1)) + r"\b", code[call.end():])
            if assignment and re.search(r"\b(?:let|var)\s*$", code[:call.end() + assignment.start()]):
                continue
            target = assignment.group(1) if assignment else ""
            def saves_collection(save):
                argument = _argument(code, save.end() - 1)
                prefix = code[:save.start()]
                if argument.strip():
                    return bool(re.search(r"\b(?:" + re.escape(target) + "|" + re.escape(call.group(1)) + r")\b", argument))
                # Empty save()/commit() on self/current owner; no unrelated service.save().
                receiver = re.search(r"\b(\w+)\s*\.\s*$", prefix)
                return receiver is None or receiver.group(1) == "self"
            save = next((save for save in STORE.finditer(code, call.end()) if saves_collection(save)), None)
            if assignment and save:
                emit("seed", unit, [call.start(), call.end() + assignment.start(), save.start()], "预置记录返回值被赋给业务集合并调用存储方法，需复核示例隔离与旧数据保护", extra=[(provider, CONSTRUCTOR.search(provider.code).start())])
        # A real fallback constructor must feed display/drawing, and avoid named placeholders.
        for asset in re.finditer(r'\?\?\s*(?:UI|NS)Image\s*\(\s*named\s*:\s*"([^"\n]+)"', clean):
            if PLACEHOLDER.search(asset.group(1)): continue
            binding = re.search(r"\b(?:let|var)\s+(\w+)\s*=\s*[^;\n]*$", clean[:asset.start()])
            if not binding: continue
            value = binding.group(1)
            display = next((call for call in re.finditer(r"\b(?:draw|Image|UIImageView|share|render|export)\s*\(", code[asset.end():], re.I)
                            if re.search(r"\b" + re.escape(value) + r"\b", _argument(code, asset.end() + call.end() - 1))), None)
            receiver = re.search(r"\b" + re.escape(value) + r"\s*\.\s*draw\s*\(", code[asset.end():])
            position = asset.end() + display.start() if display else asset.end() + receiver.start() if receiver else None
            if position is not None: emit("fallback", unit, [asset.start(), position], "用户图片缺失时回退到内置素材，并继续展示或绘制，需核对占位标识及分享输出真实性")
        # Timer -> helper -> arithmetic percentage -> terminal rendering call (same owner).
        progress = re.search(r"\b(\w*(?:progress|percent)\w*)\s*=\s*(?:min\s*\(\s*)?\1\s*\+\s*[0-9.]+|\b(\w*(?:progress|percent)\w*)\s*\+=\s*[0-9.]+", code, re.I)
        if progress:
            name = progress.group(1) or progress.group(2)
            finish = re.search(r"\bif\s+" + re.escape(name) + r"\s*>=?\s*(?:100|1(?:\.0)?)\b", code[progress.end():])
            terminal = re.search(r"\b\w*(?:finishRender|compose|generate|complete|render)\w*\s*\(", code[progress.end():], re.I)
            timer = next((other for other in units if other.owner == unit.owner and other.path == unit.path
                          and re.search(r"\bTimer\s*\.\s*scheduledTimer|\basyncAfter\s*\(", other.code)
                          and re.search(r"\b" + re.escape(unit.name) + r"\s*\(", other.code)), None)
            if finish and terminal and timer:
                trigger = re.search(r"\bTimer\s*\.\s*scheduledTimer|\basyncAfter\s*\(", timer.code)
                emit("progress", unit, [progress.start(), progress.end() + finish.start(), progress.end() + terminal.start()], "定时触发的方法按固定数值增加进度，达到阈值后调用生成/完成方法，需确认进度是否反映真实工作", extra=[(timer, trigger.start())])
        # Explicit disabled-remote branch returns success after a local publication call.
        for branch in re.finditer(r"\bguard\s+(\w*(?:remote|network|online)\w*)\s+else\s*\{", code, re.I):
            end = code.find("}", branch.end())
            if end < 0: continue
            success = re.search(r"\b\w+\s*\(\s*\.success\s*\(", code[branch.end():end])
            local = re.search(r"\b\w*(?:publishLocal|saveLocal|localSave)\w*\s*\(", code[:branch.start()], re.I)
            if success and local:
                emit("local_success", unit, [local.start(), branch.start(), branch.end() + success.start()], "远程路径不可用时先本地保存并返回成功，需确认界面准确说明仅本地结果而非在线发布")
    warnings = [x for x in found if x["status"] == "WARN"]
    status = "WARN" if warnings else "NOT_VERIFIABLE" if incomplete or found else "PASS"
    evidence = []
    for item in found:
        for e in item["evidence"]:
            if e not in evidence: evidence.append(e)
    actual = (f"发现 {len(warnings)} 条需复核风险线索" if warnings else "编译条件或源码证据不完整，无法确认风险" if status == "NOT_VERIFIABLE" else "未发现已覆盖的静态风险模式")
    return {"status": status, "severity": "high" if any(x["severity"] == "high" for x in warnings) else "medium" if status != "PASS" else "info",
            "actual": actual + "；" + LIMIT, "evidence": evidence[:12], "details": found,
            "manual_check": "核对正式包、实际展示、演示内容标识与远端行为；静态检查不替代审核结论。" if status != "PASS" else None}
