"""Client for the shared App Icon comparison and enrollment service."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


ICON_API_URL = "https://app-icon-similarity.lht841089214.chatgpt.site/api/audit"
ICON_SIZE = 128
THRESHOLD = 85
MAX_RESPONSE_BYTES = 512 * 1024


class IconApiError(RuntimeError):
    pass


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


def check_icon(pixels: bytes, app_name: str) -> tuple[list[dict], bool]:
    label = " ".join(app_name.split())[:48]
    request = Request(ICON_API_URL, data=pixels, method="POST", headers={
        "Content-Type": "application/octet-stream",
        "Accept": "application/json",
        "User-Agent": "ios-aside-review icon audit",
        "X-App-Name": quote(label, safe=""),
    })
    try:
        # Keep the image submission on the configured service.
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise IconApiError(f"图标服务返回 HTTP {error.code}，比对或入库结果待确认。") from None
    except (URLError, OSError) as error:
        raise IconApiError("图标服务不可达或响应超时，比对或入库结果待确认。") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise IconApiError("图标响应超过大小上限。")

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise IconApiError("图标服务返回无效 JSON。") from None
    if not isinstance(payload, dict) or type(payload.get("added")) is not bool:
        raise IconApiError("图标服务缺少入库结果。")
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
    added = payload["added"]
    if added != (matches[0]["score"] < THRESHOLD):
        raise IconApiError("图标服务返回的分数和入库状态不一致。")
    return matches, added
