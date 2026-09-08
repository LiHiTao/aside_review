"""Read-only effective physical source line counting for an A-side project.

This is a project policy metric, not a compiler or an App Store requirement.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

SOURCE_EXTENSIONS = {".swift", ".m", ".mm", ".h", ".c", ".cc", ".cpp", ".hpp"}
# Case-insensitive directory names. Other B-side/vendor/generated paths must be
# explicitly excluded by policy; arbitrary business folder names are not guessed.
EXCLUDED_DIRS = {
    ".git", "pods", "carthage", "build", "deriveddata", ".build", "xcuserdata",
    "node_modules", ".venv", "venv", "swiftshield-output", "sourcepackages",
    "checkouts", "vendor", "vendors", "thirdparty", "third-party", "generated",
    "generatedsources", "tests", "uitests", "unittests", "bside", "b-side", "b_side",
}


_SWIFT_STRING = re.compile(r'(#*)("""|")')
_C_STRING = re.compile(r"()(\"|')")
_CPP_RAW = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\r\n]{0,16})\(')


class LexicalError(ValueError):
    """Incomplete lexical evidence must never pass the threshold."""


def count_source_lines(text: str, suffix: str = ".swift") -> dict[str, int]:
    """Partition physical lines into code, blank and pure comment lines.

    Mask comments with spaces while preserving strings and line boundaries. Swift
    interpolation is parsed as code, including nested strings and comments.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise LexicalError("源码含 NUL 字符")
    masked = list(text)
    length = len(text)
    swift = suffix.lower() == ".swift"

    def hide(start: int, end: int) -> None:
        for pos in range(start, end):
            if text[pos] != "\n":
                masked[pos] = " "

    def string(pos: int, hashes: str, quote: str) -> int:
        pos += len(hashes) + len(quote)
        closing = quote + hashes
        escape = "\\" + hashes
        while pos < length:
            if text.startswith(closing, pos):
                return pos + len(closing)
            if text.startswith(escape, pos):
                after = pos + len(escape)
                if swift and after < length and text[after] == "(":
                    pos = code(after + 1, interpolation=True)
                else:
                    pos = min(length, after + 1)
                continue
            if text[pos] == "\n" and len(quote) == 1:
                raise LexicalError("单行字符串未闭合")
            pos += 1
        raise LexicalError("字符串未闭合")

    def code(pos: int, interpolation: bool = False) -> int:
        parentheses = 1 if interpolation else 0
        while pos < length:
            start = pos
            if text.startswith("//", pos):
                end = text.find("\n", pos)
                pos = length if end == -1 else end
                # C-family line splicing extends a line comment.
                while not swift and pos < length and text[pos - 1:pos] == "\\":
                    end = text.find("\n", pos + 1)
                    pos = length if end == -1 else end
                hide(start, pos)
                continue
            if text.startswith("/*", pos):
                pos += 2
                depth = 1
                while pos < length and depth:
                    if swift and text.startswith("/*", pos):
                        depth += 1
                        pos += 2
                    elif text.startswith("*/", pos):
                        depth -= 1
                        pos += 2
                    else:
                        pos += 1
                if depth:
                    raise LexicalError("块注释未闭合")
                hide(start, pos)
                continue
            # C++ raw strings may contain quotes, comment markers and newlines.
            raw = _CPP_RAW.match(text, pos) if not swift else None
            if raw:
                closing = ")" + raw.group(1) + '"'
                end = text.find(closing, pos + len(raw.group()))
                if end < 0:
                    raise LexicalError("C++ raw 字符串未闭合")
                pos = end + len(closing)
                continue
            opening = _SWIFT_STRING.match(text, pos) if swift else _C_STRING.match(text, pos)
            # C++ digit separators are not character literals.
            if opening and opening.group(2) == "'" and pos and text[pos - 1].isalnum() and pos + 1 < length and text[pos + 1].isalnum():
                opening = None
            if opening:
                pos = string(pos, *opening.groups())
                continue
            if interpolation:
                if text[pos] == "(":
                    parentheses += 1
                elif text[pos] == ")":
                    parentheses -= 1
                    if not parentheses:
                        return pos + 1
            pos += 1
        if interpolation:
            raise LexicalError("Swift 字符串插值未闭合")
        return pos

    code(0)
    original = text.split("\n")
    clean = "".join(masked).split("\n")
    # A terminal newline terminates the preceding physical line, not a new one.
    if not text or text.endswith("\n"):
        original.pop()
        clean.pop()
    blank = sum(not line.strip() for line in original)
    effective = sum(bool(line.strip()) for line in clean)
    return {"total_lines": len(original), "blank_lines": blank,
            "comment_lines": len(original) - blank - effective, "code_lines": effective}


def audit_code_lines(root: Path, policy: Mapping) -> dict:
    """Return CODE-001, defaulting missing or empty source scopes to the project root."""
    root = root.resolve()
    threshold = policy.get("code_line_threshold", 5000)
    result = {"id": "CODE-001", "status": "NOT_VERIFIABLE", "severity": "medium",
              "title": "A 面有效代码行数", "expected": f"A 面有效代码行数严格大于 {threshold} 行（项目自定门槛）",
              "actual": "", "evidence": [], "manual_check": None, "details": []}
    errors: list[str] = []
    scopes = policy.get("a_side_source_paths", [])
    excludes = policy.get("code_line_excluded_paths", [])
    ignored = policy.get("ignored_paths", [])
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        errors.append("code_line_threshold 必须是非负整数")
    for name, value in (("a_side_source_paths", scopes), ("code_line_excluded_paths", excludes), ("ignored_paths", ignored)):
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"{name} 必须是非空路径字符串组成的数组")
    if isinstance(excludes, list) and all(isinstance(item, str) for item in excludes):
        for item in excludes:
            if Path(item).is_absolute() or ".." in Path(item).parts or any(char in item for char in "*?[]"):
                errors.append("code_line_excluded_paths 必须是根目录内的相对精确路径，不支持通配符")
    if errors:
        result["actual"] = "；".join(errors)
        result["manual_check"] = "修正策略配置后重新扫描。"
        return result
    # A normal project is the default A-side scope. Normalize only after
    # validation so malformed explicit scopes never fall back to the root.
    if not scopes:
        scopes = ["."]

    def excluded(path: Path) -> bool:
        relative = path.relative_to(root)
        if any(part.lower() in EXCLUDED_DIRS or part.endswith(("Tests", "UITests")) for part in relative.parts[:-1]):
            return True
        if path.is_dir() and (path.name.lower() in EXCLUDED_DIRS or path.name.endswith(("Tests", "UITests"))):
            return True
        stem = path.stem.lower()
        if path.stem.endswith(("Tests", "Test")) or stem.startswith("test_") or stem.endswith(".generated"):
            return True
        value = relative.as_posix()
        for pattern in excludes:
            normalized = Path(pattern).as_posix()
            if normalized == "." or value == normalized or value.startswith(normalized + "/"):
                return True
        if any(pattern in relative.parts for pattern in ignored):
            return True
        return False

    files: dict[Path, Path] = {}

    def add_file(path: Path) -> None:
        if excluded(path) or path.suffix.lower() not in SOURCE_EXTENSIONS:
            return
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                errors.append(f"{path.relative_to(root).as_posix()}：源码链接指向审计根目录外")
            elif not excluded(resolved) and resolved.is_file():
                files.setdefault(resolved, path)
        except (OSError, RuntimeError):
            errors.append(f"{path.relative_to(root).as_posix()}：源码路径无法读取")

    for scope in scopes:
        relative = Path(scope)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append("A 面范围必须是审计根目录内的相对路径")
            continue
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                errors.append(f"{scope}：范围指向审计根目录外")
                continue
            if any((root.joinpath(*relative.parts[:index + 1])).is_symlink() for index in range(len(relative.parts))) and resolved.is_dir():
                errors.append(f"{scope}：范围是目录符号链接，请直接指定真实目录")
                continue
            if excluded(candidate):
                continue
            if candidate.is_file():
                if candidate.suffix.lower() not in SOURCE_EXTENSIONS:
                    errors.append(f"{scope}：不是支持的源码文件")
                else:
                    add_file(candidate)
            elif candidate.is_dir():
                def walk_error(error: OSError) -> None:
                    errors.append(f"{scope}：目录遍历失败（{type(error).__name__}）")
                for directory, directories, names in os.walk(candidate, followlinks=False, onerror=walk_error):
                    base = Path(directory)
                    directories[:] = sorted(name for name in directories if not (base / name).is_symlink() and not excluded(base / name))
                    for name in sorted(names):
                        add_file(base / name)
            else:
                errors.append(f"{scope}：不是普通文件或目录")
        except (OSError, RuntimeError):
            errors.append(f"{scope}：范围不存在或无法访问")

    total = 0
    for resolved, path in sorted(files.items(), key=lambda item: item[1].relative_to(root).as_posix()):
        label = path.relative_to(root).as_posix()
        try:
            metrics = count_source_lines(resolved.read_text(encoding="utf-8-sig"), path.suffix)
        except (OSError, UnicodeError, LexicalError, RecursionError) as error:
            errors.append(f"{label}：源码读取或词法解析失败（{type(error).__name__}）")
            result["details"].append({"label": label, "status": "NOT_VERIFIABLE", "actual": "源码读取或词法解析失败"})
            continue
        total += metrics["code_lines"]
        summary = f"有效代码 {metrics['code_lines']} 行；总计 {metrics['total_lines']} 行，空行 {metrics['blank_lines']} 行，纯注释 {metrics['comment_lines']} 行"
        result["details"].append({"label": label, "path": label, "status": "PASS", "actual": summary, **metrics})
        result["evidence"].append({"path": label, "line": 1, "excerpt": summary})
    if errors:
        result["actual"] = f"统计不完整，已计有效代码 {total:,} 行；" + "；".join(errors)
        result["manual_check"] = "明确 A 面目录、修正无法读取或解析的文件及范围配置后重新扫描。"
    else:
        result["status"] = "PASS" if total > threshold else "FAIL"
        result["severity"] = "info" if total > threshold else "high"
        result["actual"] = f"A 面有效代码 {total:,} 行，{len(files)} 个文件；要求 > {threshold:,} 行"
    return result
