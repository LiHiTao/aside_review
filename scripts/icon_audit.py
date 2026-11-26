"""Compare configured App Icons, then enroll only after a complete audit gate."""
from __future__ import annotations

import hashlib
from pathlib import Path
import plistlib
import re
from typing import Iterable, Mapping

try:
    from .app_icon_assets import _DEFAULT_IGNORED, _clean_build, find_app_icons
    from .icon_api import THRESHOLD, MAX_BATCH_ICONS, IconApiError, IconEnrollmentConflict, compare_icon, enroll_icons, icon_pixels
except ImportError:
    from app_icon_assets import _DEFAULT_IGNORED, _clean_build, find_app_icons
    from icon_api import THRESHOLD, MAX_BATCH_ICONS, IconApiError, IconEnrollmentConflict, compare_icon, enroll_icons, icon_pixels


def icon_project_key(root: Path, build_sources: Mapping[Path, str], ignored: Iterable[str] = ()) -> str:
    """Hash one unambiguous literal bundle ID, otherwise the resolved root path."""
    root = root.resolve()
    identifiers = set()
    unresolved = False
    ignored_names = _DEFAULT_IGNORED | {str(value).strip('/') for value in ignored}
    for path, raw in build_sources.items():
        path = Path(path)
        if not path.is_absolute():
            path = root / path
        if not path.resolve().is_relative_to(root):
            continue
        relative = path.resolve().relative_to(root)
        if any(name in relative.parts or relative.as_posix().startswith(name + '/')
               for name in ignored_names if name):
            continue
        values = []
        if path.name == 'project.pbxproj' or path.suffix == '.xcconfig':
            for match in re.finditer(r'(?m)^\s*"?PRODUCT_BUNDLE_IDENTIFIER(?:\[[^\]\n]+\])*"?\s*=\s*("(?:\\.|[^"\\])*"|[^;\n]*)', _clean_build(raw)):
                value = match[1].strip().strip('"')
                if re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', value):
                    values.append(value)
                else:
                    unresolved = True
        elif path.suffix == '.plist':
            try:
                contents = plistlib.loads(raw.encode())
            except (ValueError, TypeError, plistlib.InvalidFileException):
                continue
            if not isinstance(contents, dict):
                continue
            value = contents.get('CFBundleIdentifier')
            # A standard plist reference delegates identity to build settings.
            if value in ('$(PRODUCT_BUNDLE_IDENTIFIER)', '${PRODUCT_BUNDLE_IDENTIFIER}', None):
                continue
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', value):
                values.append(value)
            else:
                unresolved = True
        identifiers.update(values)
    identity = 'bundle\0' + next(iter(identifiers)) if len(identifiers) == 1 and not unresolved else 'path\0' + str(root)
    return hashlib.sha256(identity.encode()).hexdigest()


def audit_app_icons(root: Path, build_sources: Mapping[Path, str], ignored: Iterable[str], *,
                    enrollment_allowed: bool = False) -> dict:
    root = root.resolve()
    ignored = tuple(ignored)
    finding = {
        "id": "IOS-003",
        "title": "App 图标相似度",
        "expected": f"与共享图标库比对的最高相似度不超过 {THRESHOLD}%",
        "status": "NOT_VERIFIABLE",
        "severity": "medium",
        "evidence": [],
        "details": [],
        "manual_check": "确认 App 主图标和图标服务可用后重新扫描。",
    }
    icons, issues = find_app_icons(root, build_sources, ignored)
    if not icons:
        return {**finding, "actual": "。".join(issues) or "未找到可比对的 App 主图标"}
    project_key = icon_project_key(root, build_sources, ignored)
    seen = set()
    matches_by_icon = []
    candidates = []
    for icon in icons:
        relative = icon.relative_to(root).as_posix()
        try:
            pixels = icon_pixels(icon)
        except ImportError:
            return {**finding, "actual": "当前 Python 缺少 Pillow，无法读取图标。"}
        except (OSError, ValueError):
            issues.append(f"{relative} 无法作为 App 图标读取")
            continue
        if pixels in seen:
            continue
        seen.add(pixels)
        try:
            matches, already_enrolled = compare_icon(pixels, root.name, project_key)
        except IconApiError as error:
            issues.append(f"{relative} {error}")
            continue
        best = matches[0]
        score = best["score"]
        actual = f'最高 {score:.1f}%（{best["name"]}）'
        evidence = {"path": relative, "excerpt": actual}
        finding["evidence"].append(evidence)
        detail = {
            "label": relative, "status": "FAIL" if score > THRESHOLD else "PASS",
            "actual": actual, "evidence": [evidence], "matches": matches, "added": False,
            "already_enrolled": already_enrolled,
            "enrollment_status": "already_enrolled" if already_enrolled else "not_required" if score >= THRESHOLD else "skipped",
        }
        matches_by_icon.append(detail)
        if score < THRESHOLD and not already_enrolled:
            candidates.append((pixels, detail))

    can_enroll = enrollment_allowed and not issues and all(item['status'] == 'PASS' for item in matches_by_icon)
    if candidates:
        reason = '整体审计存在 FAIL 或 NOT_VERIFIABLE，跳过入库。'
        if enrollment_allowed and not can_enroll:
            reason = '图标比对存在超阈值或未完成项，跳过入库。'
        elif can_enroll and len(candidates) > MAX_BATCH_ICONS:
            reason = f'待入库图标超过单批 {MAX_BATCH_ICONS} 个，跳过入库。'
            can_enroll = False
        if can_enroll:
            try:
                added = enroll_icons([pixels for pixels, _ in candidates], root.name, project_key)
                for (_, detail), enrolled in zip(candidates, added):
                    detail.update(added=enrolled, already_enrolled=not enrolled,
                                  enrollment_status='confirmed' if enrolled else 'already_enrolled')
            except IconApiError as error:
                reason = str(error)
                issues.append('批量入库结果待复核：' + reason)
                for _, detail in candidates:
                    detail['enrollment_status'] = 'rejected' if isinstance(error, IconEnrollmentConflict) else 'uncertain'
        for _, detail in candidates:
            if detail['enrollment_status'] in {'skipped', 'uncertain', 'rejected'}:
                detail['enrollment_reason'] = reason
    enrollment_labels = {'already_enrolled': '本工程图标已入库', 'not_required': '无需入库',
                         'skipped': '已跳过入库', 'confirmed': '已确认入库',
                         'rejected': '入库被拒绝，需重新比对', 'uncertain': '入库结果待确认'}
    for detail in matches_by_icon:
        detail['actual'] += '，' + enrollment_labels[detail['enrollment_status']]
        if detail.get('enrollment_reason'):
            detail['actual'] += '；' + detail['enrollment_reason']
    finding["details"] = list(matches_by_icon)
    if issues:
        finding["details"].append({"label": "覆盖范围", "status": "NOT_VERIFIABLE",
                                   "actual": "。".join(issues)})
    if not matches_by_icon:
        return {**finding, "actual": "。".join(issues)}
    highest = max(matches_by_icon, key=lambda item: item["matches"][0]["score"])
    enrolled = sum(item["added"] for item in matches_by_icon)
    existing = sum(item['enrollment_status'] == 'already_enrolled' for item in matches_by_icon)
    finding["actual"] = (
        f'{highest["actual"]}。已比对 {len(matches_by_icon)} 个图标，入库 {enrolled} 个，已在库 {existing} 个。'
        + ("部分图标或入库结果需复核。" if issues else "")
    )
    reasons = list(dict.fromkeys(item['enrollment_reason'] for item in matches_by_icon
                                if item.get('enrollment_reason')))
    if reasons and highest.get('enrollment_reason') not in reasons:
        finding['actual'] += '。'.join(reasons)
    if highest["status"] == "FAIL":
        finding.update(status="FAIL", severity="high", manual_check="核对主图标和最高分参考图标。")
    elif not issues:
        finding.update(status="PASS", severity="info", manual_check=None)
    return finding
