"""Client for the shared App Icon comparison and enrollment service."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


ICON_API_URL = "https://app-icon-audit.vercel.app/api/audit"
ICON_COMPARE_URL = ICON_API_URL + "/compare"
ICON_ENROLL_URL = ICON_API_URL + "/enroll"
LEGACY_ICON_API_URL = "https://app-icon-similarity.lht841089214.chatgpt.site/api/audit"
ICON_SIZE = 128
PIXEL_BYTES = ICON_SIZE * ICON_SIZE * 3
MAX_BATCH_ICONS = 16
THRESHOLD = 85
MAX_RESPONSE_BYTES = 512 * 1024


class IconApiError(RuntimeError):
    pass


class IconEnrollmentConflict(IconApiError):
    """The server rejected the batch before updating the index."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def icon_pixels(path: Path) -> bytes:
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as source:
            if not all(32 <= side <= 4096 for side in source.size) or max(source.size) / min(source.size) > 1.35:
                raise ValueError("图标需接近正方形且宽高在 32 至 4096 像素之间")
            rgba = ImageOps.exif_transpose(source).convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            return background.convert("RGB").resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS).tobytes()
    except Image.DecompressionBombError as error:
        raise ValueError("图标像素数超过读取上限") from error


def _post(url: str, pixels: bytes, app_name: str, project_key: str | None = None) -> dict:
    label = " ".join(app_name.split())[:48]
    headers = {
        "Content-Type": "application/octet-stream",
        "Accept": "application/json",
        "User-Agent": "ios-aside-review icon audit",
        "X-App-Name": quote(label, safe=""),
    }
    if project_key is not None:
        if not isinstance(project_key, str) or not re.fullmatch(r"[a-f0-9]{64}", project_key):
            raise IconApiError("图标项目标识无效。")
        headers["X-App-Project"] = project_key
    request = Request(url, data=pixels, method="POST", headers=headers)
    try:
        # Keep the image submission on the configured service.
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        error.close()
        if error.code == 409:
            raise IconEnrollmentConflict("图标参考库发生变化，入库被拒绝且索引未更新，请重新比对。") from None
        raise IconApiError(f"图标服务返回 HTTP {error.code}，比对或入库结果待确认。") from None
    except (URLError, OSError) as error:
        raise IconApiError("图标服务不可达或响应超时，比对或入库结果待确认。") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise IconApiError("图标响应超过大小上限。")

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise IconApiError("图标服务返回无效 JSON。") from None
    if not isinstance(payload, dict):
        raise IconApiError("图标服务响应格式无效。")
    return payload


def _matches(payload: dict) -> list[dict]:
    rows = payload.get("matches")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
        raise IconApiError("图标服务未返回有效匹配。")
    matches = []
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        score = row.get("score") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name.strip() or type(score) not in (int, float) or not 0 <= score <= 100:
            raise IconApiError("图标服务返回无效匹配。")
        matches.append({"name": " ".join(name.split())[:100], "score": score})
    matches.sort(key=lambda row: -row["score"])
    return matches


def check_icon(pixels: bytes, app_name: str) -> tuple[list[dict], bool]:
    """Legacy compare-and-enroll request retained for existing integrations."""
    payload = _post(LEGACY_ICON_API_URL, pixels, app_name)
    if type(payload.get("added")) is not bool:
        raise IconApiError("图标服务缺少入库结果。")
    matches = _matches(payload)
    added = payload["added"]
    if added != (matches[0]["score"] < THRESHOLD):
        raise IconApiError("图标服务返回的分数和入库状态不一致。")
    return matches, added


def compare_icon(pixels: bytes, app_name: str, project_key: str) -> tuple[list[dict], bool]:
    """Compare without enrolling and retain exact same-project enrollment state."""
    if not isinstance(pixels, bytes) or len(pixels) != PIXEL_BYTES:
        raise IconApiError("图标像素数据大小无效。")
    payload = _post(ICON_COMPARE_URL, pixels, app_name, project_key)
    matches = _matches(payload)
    if payload.get("added") is not False or type(payload.get("already_enrolled")) is not bool:
        raise IconApiError("图标服务返回的只读比对状态无效。")
    return matches, payload["already_enrolled"]


def enroll_icons(images: Sequence[bytes], app_name: str, project_key: str) -> list[bool]:
    """Enroll one complete batch after the caller's audit gate has passed."""
    if not 1 <= len(images) <= MAX_BATCH_ICONS or any(
        not isinstance(pixels, bytes) or len(pixels) != PIXEL_BYTES for pixels in images
    ):
        raise IconApiError(f"图标入库需提供 1 至 {MAX_BATCH_ICONS} 个完整图像。")
    payload = _post(ICON_ENROLL_URL, b"".join(images), app_name, project_key)
    added = payload.get("added")
    if not isinstance(added, list) or len(added) != len(images) or any(type(value) is not bool for value in added):
        raise IconApiError("图标服务返回的批量入库结果无效，入库状态待确认。")
    return added
