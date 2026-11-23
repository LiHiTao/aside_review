"""Read-only discovery of configured default iOS app-icon images.

This module does not build the app, infer target membership, or select arbitrary
images. Callers must retain the returned issues even when some icons resolve.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Iterable, Mapping


_DEFAULT_IGNORED = {".git", "Pods", "Carthage", "build", "DerivedData", ".build",
                    "xcuserdata", "node_modules", "swiftshield-output"}
_SETTING = re.compile(
    r'(?:"ASSETCATALOG_COMPILER_APPICON_NAME(?:\[[^\]\n]+\])*"|'
    r'ASSETCATALOG_COMPILER_APPICON_NAME(?:\[[^\]\n]+\])*)\s*=\s*'
    r'("(?:\\.|[^"\\])*"|[^;\n]*)')
# Preserve string literals, in particular paths containing //, while removing
# build-file comments. Keeping line breaks makes malformed settings detectable.
_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\n]*')


def _clean_build(text: str) -> str:
    return _COMMENT.sub(lambda m: m[0] if m[0].startswith('"') else
                        "\n" * m[0].count("\n"), text)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _rank(image: dict) -> tuple[int, float] | None:
    """Rank a declared default iOS image; do not borrow dark/tinted variants."""
    if image.get("appearances"):
        return None
    if image.get("platform") not in (None, "ios"):
        return None
    idiom = image.get("idiom", "universal")
    if idiom not in ("iphone", "ipad", "ios-marketing", "universal"):
        return None
    size = image.get("size", "")
    scale = image.get("scale", "1x")
    if not isinstance(size, str) or not isinstance(scale, str):
        return None
    dimensions = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", size)
    multiplier = re.fullmatch(r"([123])x", scale)
    if not dimensions or not multiplier:
        return None
    width, height = map(float, dimensions.groups())
    if width <= 0 or width != height:
        return None
    pixels = width * int(multiplier[1])
    preferred = idiom in ("universal", "ios-marketing") and pixels == 1024
    return int(preferred), pixels * pixels


def _asset_image(asset: Path, root: Path) -> tuple[Path | None, list[str]]:
    label = str(asset.relative_to(root))
    contents = asset / "Contents.json"
    if not _inside(contents, root):
        return None, [f"图标目录 {label} 的 Contents.json 指向工程外部。"]
    try:
        payload = json.loads(contents.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError) as exc:
        return None, [f"无法读取图标目录 {label} 的 Contents.json：{type(exc).__name__}。"]
    if not isinstance(payload, dict) or not isinstance(payload.get("images"), list):
        return None, [f"图标目录 {label} 的 images 清单无效。"]
    candidates = []
    issues = []
    for entry in payload["images"]:
        if not isinstance(entry, dict):
            issues.append(f"图标目录 {label} 包含无效的图片记录。")
            continue
        # Unassigned Xcode slots and intentionally unsupported variants are not
        # missing files. A declared filename, however, must really exist.
        if not entry.get("filename"):
            continue
        if entry.get("appearances") or entry.get("platform") not in (None, "ios") or entry.get("idiom", "universal") not in ("iphone", "ipad", "ios-marketing", "universal"):
            continue
        filename = entry["filename"]
        if not isinstance(filename, str) or Path(filename).name != filename or filename in (".", ".."):
            issues.append(f"图标目录 {label} 包含无效的图片文件名。")
            continue
        candidate = asset / filename
        if not _inside(candidate, root) or not _inside(candidate, asset.resolve()):
            issues.append(f"图标目录 {label} 的图片 {filename} 指向图标目录外部。")
            continue
        if not candidate.is_file():
            issues.append(f"图标目录 {label} 缺少图片 {filename}。")
            continue
        rank = _rank(entry)
        if rank is None:
            issues.append(f"图标目录 {label} 的图片 {filename} 尺寸或缩放信息无法解析。")
            continue
        candidates.append((rank, candidate))
    if not candidates:
        issues.append(f"图标目录 {label} 没有可用的默认 iOS 图标图片。")
        return None, issues
    best_rank = max(rank for rank, _ in candidates)
    best = sorted({path for rank, path in candidates if rank == best_rank})
    if len(best) > 1:
        # Different iPhone/iPad defaults at the same resolution are not safely
        # interchangeable for image similarity checks.
        issues.append(f"图标目录 {label} 存在多个同等优先级的默认图标，无法唯一选择。")
        return None, issues
    return best[0], issues


def find_app_icons(root: Path, build_sources: Mapping[Path, str],
                   ignored: Iterable[str]) -> tuple[list[Path], list[str]]:
    """Return selected images and unresolved issues, without modifying files.

    Explicit ASSETCATALOG_COMPILER_APPICON_NAME values in pbxproj/xcconfig win.
    All explicitly named sets are inspected; duplicate matching sets are
    ambiguous. Without settings, prefer one AppIcon set or one total candidate.
    CFBundleIconName is deliberately not used without target/plist association.
    """
    root = Path(root).resolve()
    ignored_names = _DEFAULT_IGNORED | {str(value).strip("/") for value in ignored}

    def is_ignored(path: Path) -> bool:
        relative = path.relative_to(root)
        return any(name in relative.parts or str(relative) == name or
                   str(relative).startswith(name + "/") for name in ignored_names if name)

    issues: list[str] = []
    names: set[str] = set()
    configured = False
    for path, text in build_sources.items():
        path = Path(path)
        if path.name != "project.pbxproj" and path.suffix != ".xcconfig":
            continue
        if not path.is_absolute():
            path = root / path
        if not _inside(path, root):
            issues.append("图标构建配置指向工程外部，无法解析。")
            continue
        path = path.resolve()
        if is_ignored(path):
            continue
        for match in _SETTING.finditer(_clean_build(text)):
            configured = True
            value = match[1].strip().strip('"').strip()
            if not value or re.search(r"[$\\\n\r]", value) or "/" in value or value in (".", ".."):
                issues.append(f"图标构建配置 {path.relative_to(root)} 的 AppIcon 名称为空或包含未解析表达式。")
                continue
            names.add(value)
    assets: dict[str, list[Path]] = {}
    if not root.is_dir():
        return [], ["目标工程目录不存在或不可读取。"]
    def walk_error(error: OSError) -> None:
        issues.append(f"图标目录扫描不完整：{type(error).__name__}。")
    for current, directories, _ in os.walk(root, followlinks=False, onerror=walk_error):
        parent = Path(current)
        keep = []
        for name in sorted(directories):
            path = parent / name
            if is_ignored(path):
                continue
            if name.endswith(".appiconset"):
                assets.setdefault(name[:-11], []).append(path)
                continue
            if path.is_symlink():
                # Do not follow directory links: cycles and external catalogs
                # cannot supply reliable project-owned resource evidence.
                if name.endswith(".xcassets"):
                    issues.append(f"资源目录 {path.relative_to(root)} 为符号链接，未扫描。")
                continue
            keep.append(name)
        directories[:] = keep
    if not configured:
        if len(assets.get("AppIcon", [])) == 1:
            names = {"AppIcon"}
        elif sum(map(len, assets.values())) == 1:
            names = set(assets)
        elif assets:
            issues.append("未配置主 AppIcon 名称且存在多个候选图标目录，无法唯一选择。")
        else:
            issues.append("未找到 AppIcon 构建配置或 appiconset 图标目录。")
    selected: list[Path] = []
    for name in sorted(names):
        matches = assets.get(name, [])
        if not matches:
            issues.append(f"配置的主图标 {name} 没有对应的 appiconset 目录。")
        elif len(matches) != 1:
            issues.append(f"配置的主图标 {name} 对应多个 appiconset 目录，无法唯一选择。")
        elif not _inside(matches[0], root):
            issues.append(f"配置的主图标 {name} 指向工程外部。")
        else:
            image, errors = _asset_image(matches[0], root)
            issues.extend(errors)
            if image is not None:
                selected.append(image)
    return list(dict.fromkeys(selected)), list(dict.fromkeys(issues))
