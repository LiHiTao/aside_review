"""Run the shared icon check on the project's configured App Icons."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

try:
    from .app_icon_assets import find_app_icons
    from .icon_api import THRESHOLD, IconApiError, check_icon, icon_pixels, read_token
except ImportError:
    from app_icon_assets import find_app_icons
    from icon_api import THRESHOLD, IconApiError, check_icon, icon_pixels, read_token


def audit_app_icons(root: Path, build_sources: Mapping[Path, str], ignored: Iterable[str]) -> dict:
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
    try:
        token = read_token()
    except IconApiError as error:
        return {**finding, "actual": str(error)}

    seen = set()
    matches_by_icon = []
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
            matches, added = check_icon(pixels, root.name, token)
        except IconApiError as error:
            issues.append(f"{relative} {error}")
            continue
        best = matches[0]
        score = best["score"]
        actual = f'最高 {score:.1f}%（{best["name"]}）'
        evidence = {"path": relative, "excerpt": actual}
        finding["evidence"].append(evidence)
        matches_by_icon.append({
            "label": relative, "status": "FAIL" if score > THRESHOLD else "PASS",
            "actual": actual + ("，已入库" if added else "，未入库"),
            "evidence": [evidence], "matches": matches, "added": added,
        })

    finding["details"] = list(matches_by_icon)
    if issues:
        finding["details"].append({"label": "覆盖范围", "status": "NOT_VERIFIABLE",
                                   "actual": "。".join(issues)})
    if not matches_by_icon:
        return {**finding, "actual": "。".join(issues)}
    highest = max(matches_by_icon, key=lambda item: item["matches"][0]["score"])
    enrolled = sum(item["added"] for item in matches_by_icon)
    finding["actual"] = (
        f'{highest["actual"]}。已比对 {len(matches_by_icon)} 个图标，入库 {enrolled} 个。'
        + ("部分图标需复核。" if issues else "")
    )
    if highest["status"] == "FAIL":
        finding.update(status="FAIL", severity="high", manual_check="核对主图标和最高分参考图标。")
    elif not issues:
        finding.update(status="PASS", severity="info", manual_check=None)
    return finding
