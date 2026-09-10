#!/usr/bin/env python3
"""Read-only static audit for iOS A-side App Store review risks.

The scanner uses only Python's standard library for discovery and JSON/Markdown
reports. PDF output is loaded lazily and never changes the audited project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


try:
    from .update_skill import UpdateError, ensure_latest
    from .restore_detection import detect_restore
    from .launch_membership import evaluate_membership
    from .code_lines import audit_code_lines
    from .product_context import identity_fields
    from .legal_links import analyze_legal_links
except ImportError:
    from update_skill import UpdateError, ensure_latest
    from restore_detection import detect_restore
    from launch_membership import evaluate_membership
    from code_lines import audit_code_lines
    from product_context import identity_fields
    from legal_links import analyze_legal_links


DEFAULT_POLICY: dict[str, Any] = {
    "required_prices_usd": ["0.99", "2.99", "9.99", "19.99", "49.99", "99.99"],
    "allow_extra_tiers": True,
    "require_submit_for_review": True,
    "app_name_min_letters": 4,
    "app_name_max_letters": 7,
    "description_max_chars": 55,
    "purpose_min_chars": 20,
    "forbid_restore": True,
    "forbid_notification_delegate": True,
    "forbid_privacy_manifest": True,
    "sensitive_terms": {
        "AI/人工智能": [
            "AI",
            "artificial intelligence",
            "人工智能",
            "generative AI",
            "OpenAI",
            "ChatGPT",
            "Gemini",
            "Claude",
            "Anthropic",
            "LLM",
        ],
        "Dating/交友": [
            "dating",
            "dating app",
            "online dating",
            "matchmaking",
            "hookup",
            "romance app",
            "约会",
            "交友",
            "相亲",
        ],
        "赌博/博彩": [
            "gambling",
            "casino",
            "betting",
            "wager",
            "sportsbook",
            "poker",
            "slot machine",
            "roulette",
            "blackjack",
            "lottery",
            "jackpot",
            "赌博",
            "博彩",
            "赌场",
            "下注",
        ],
        "色情/成人内容": [
            "porn",
            "pornography",
            "xxx",
            "adult content",
            "sexual",
            "nude",
            "nudity",
            "erotic",
            "explicit content",
            "OnlyFans",
            "色情",
            "淫秽",
            "成人内容",
            "性行为",
            "裸体",
            "情色",
        ],
    },
    "ignored_paths": [],
    "a_side_source_paths": [],
    "code_line_excluded_paths": [],
    "code_line_threshold": 5000,
}

IGNORED_DIRS = {
    ".git",
    "Pods",
    "Carthage",
    "build",
    "Build",
    "DerivedData",
    ".build",
    "xcuserdata",
    "node_modules",
    ".venv",
    "venv",
    "swiftshield-output",
}

SOURCE_EXTENSIONS = {".swift", ".m", ".mm", ".h", ".c", ".cc", ".cpp", ".xcconfig", ".pbxproj"}
LEGAL_INPUT_EXTENSIONS = {".swift", ".m", ".mm", ".h", ".strings", ".xcstrings"}
TEXT_EXTENSIONS = SOURCE_EXTENSIONS | {
    ".json",
    ".plist",
    ".entitlements",
    ".strings",
    ".xcstrings",
    ".storyboard",
    ".xcprivacy",
    ".xib",
    ".txt",
    ".md",
    ".html",
    ".htm",
    ".yaml",
    ".yml",
}

PRODUCT_ID_KEYS = ("product_id", "productId", "productID")
PRICE_KEYS = ("price_usd", "priceUSD", "price", "configuredPrice", "configured_price")
NAME_KEYS = ("reference_name", "referenceName", "name", "title")

IAP_CONTAINER_KEYS = {"iap_products", "iapProducts", "in_app_purchases", "inAppPurchases", "products"}

PURPOSE_SPECS: dict[str, dict[str, Any]] = {
    "camera": {
        "label": "相机",
        "keys": ("NSCameraUsageDescription",),
        "patterns": (r"AVCaptureDevice", r"UIImagePickerController", r"\.camera\b", r"requestAccess\s*\(\s*for:\s*\.video"),
        "objects": ("camera", "photo", "profile", "相机", "照片"),
        "actions": ("take", "capture", "撮", "拍", "camera", "使用"),
    },
    "photo_library": {
        "label": "相册",
        "keys": ("NSPhotoLibraryUsageDescription", "NSPhotoLibraryAddUsageDescription"),
        "patterns": (r"PHPhotoLibrary", r"PhotosPicker", r"PHPicker", r"UIImagePickerController", r"UIImageWriteToSavedPhotosAlbum", r"\.photoLibrary\b"),
        "objects": ("photo", "photos", "photo library", "相册", "照片"),
        "actions": ("choose", "select", "save", "add", "browse", "选择", "保存", "添加"),
    },
    "microphone": {
        "label": "麦克风",
        "keys": ("NSMicrophoneUsageDescription",),
        "patterns": (r"AVAudioSession", r"requestRecordPermission", r"\.audio\b", r"SFSpeechRecognizer"),
        "objects": ("microphone", "voice", "audio", "麦克风", "语音", "音频"),
        "actions": ("record", "dictate", "speak", "convert", "录", "说", "转换", "识别"),
    },
    "tracking": {
        "label": "ATT",
        "keys": ("NSUserTrackingUsageDescription",),
        "patterns": (r"AppTrackingTransparency", r"ATTrackingManager", r"advertisingIdentifier"),
        "objects": ("tracking", "advertising", "追踪", "广告"),
        "actions": ("measure", "improve", "personal", "track", "衡量", "改进", "追踪"),
    },
    "push": {
        "label": "Push",
        "keys": (),
        "patterns": (r"UserNotifications", r"requestAuthorization\s*\(\s*options", r"registerForRemoteNotifications"),
        "objects": ("notification", "alert", "提醒", "通知"),
        "actions": ("notify", "remind", "receive", "通知", "提醒", "接收"),
    },
}

AI_PATTERNS = (
    r"api\.openai\.com",
    r"openai",
    r"anthropic",
    r"api\.anthropic\.com",
    r"generativeai",
    r"gemini",
    r"chatgpt",
    r"claude",
    r"llm",
)
PERSONAL_DATA_PATTERNS = (
    r"photo",
    r"image",
    r"video",
    r"audio",
    r"voice",
    r"microphone",
    r"profile",
    r"email",
    r"phone",
    r"location",
    r"device",
    r"用户",
    r"照片",
    r"头像",
    r"语音",
)

STATUS_ORDER = ("PASS", "FAIL", "NOT_VERIFIABLE", "WARN")
STATUS_PRIORITY = {"PASS": 0, "NOT_VERIFIABLE": 1, "WARN": 2, "FAIL": 3}
SEVERITY_PRIORITY = {"info": 0, "low": 1, "medium": 2, "high": 3, "blocker": 4}

RULE_ORDER = (
    "CODE-001",
    "IAP-SUMMARY",
    "AB-001",
    "IAP-009",
    "PERM-001",
    "PERM-002",
    "ATT-001",
    "ATT-002",
    "LEGAL-001",
    "PRIV-001",
    "META-001",
    "META-002",
    "META-003",
    "PRIV-002",
    "SENSITIVE-001",
    "IOS-001",
    "IOS-002",
)

RULE_TITLES = {
    "CODE-001": "A 面有效代码行数",
    "IAP-SUMMARY": "内购项统一检查",
    "AB-001": "A 面通知代理",
    "IAP-009": "恢复购买入口",
    "PERM-001": "权限 Xcode 配置",
    "PERM-002": "权限用途文案",
    "ATT-001": "ATT Xcode 配置",
    "ATT-002": "ATT 用途文案",
    "LEGAL-001": "协议打开方式",
    "PRIV-001": "第三方 AI 数据共享",
    "META-001": "商店应用描述",
    "META-002": "免费与价格声明",
    "META-003": "IAP 提交状态",
    "PRIV-002": "隐私清单文件",
    "SENSITIVE-001": "敏感词检查",
    "IOS-001": "LaunchScreen",
    "IOS-002": "App 名称",
}


@dataclass
class ProductRecord:
    path: Path
    pointer: str
    product_id: str | None = None
    price: str | None = None
    reference_name: str | None = None
    names: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    submit_for_review: Any = None


@dataclass
class CodeProduct:
    path: Path
    line: int
    product_id: str
    price: str | None = None
    names: list[str] = field(default_factory=list)


def normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def as_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    return normalise_space(str(value))


def parse_price(value: Any) -> str | None:
    text = as_string(value)
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d+(?:[.,]\d{1,2})?)(?!\d)", text.replace(",", "."))
    if not match:
        return None
    try:
        return f"{Decimal(match.group(1)):.2f}"
    except InvalidOperation:
        return None


def first_value(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def flatten_text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [normalise_space(value)] if value.strip() else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(flatten_text_values(item))
        return result
    if isinstance(value, dict):
        result = []
        for key in ("name", "title", "description", "text", "value"):
            if key in value:
                result.extend(flatten_text_values(value[key]))
        return result
    return []


def iter_dicts(value: Any, pointer: str = "$") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield pointer, value
        for key, child in value.items():
            yield from iter_dicts(child, f"{pointer}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_dicts(child, f"{pointer}[{index}]")


def is_product_dict(mapping: Mapping[str, Any]) -> bool:
    product_id = first_value(mapping, PRODUCT_ID_KEYS)
    return product_id is not None and any(
        key in mapping for key in (*PRICE_KEYS, *NAME_KEYS, "localizations", "type", "submit_for_review")
    )


def extract_product_records(path: Path, data: Any) -> list[ProductRecord]:
    records: list[ProductRecord] = []
    seen: set[int] = set()
    for pointer, mapping in iter_dicts(data):
        if id(mapping) in seen or not is_product_dict(mapping):
            continue
        seen.add(id(mapping))
        raw_id = first_value(mapping, PRODUCT_ID_KEYS)
        product_id = as_string(raw_id)
        names: list[str] = []
        reference_name = as_string(first_value(mapping, ("reference_name", "referenceName")))
        if reference_name:
            names.append(reference_name)
        direct_name = as_string(first_value(mapping, ("name", "title")))
        if direct_name:
            names.append(direct_name)
        localisations = mapping.get("localizations")
        if isinstance(localisations, list):
            for localisation in localisations:
                if isinstance(localisation, dict):
                    for key in ("name", "title"):
                        value = as_string(localisation.get(key))
                        if value:
                            names.append(value)
        descriptions: list[str] = []
        direct_description = as_string(mapping.get("description"))
        if direct_description:
            descriptions.append(direct_description)
        if isinstance(localisations, list):
            for localisation in localisations:
                if isinstance(localisation, dict):
                    value = as_string(localisation.get("description"))
                    if value:
                        descriptions.append(value)
        records.append(
            ProductRecord(
                path=path,
                pointer=pointer,
                product_id=product_id,
                price=parse_price(first_value(mapping, PRICE_KEYS)),
                reference_name=reference_name,
                names=list(dict.fromkeys(names)),
                descriptions=list(dict.fromkeys(descriptions)),
                submit_for_review=mapping.get("submit_for_review"),
            )
        )
    return records


def mask_comments(text: str) -> str:
    """Mask C/Swift comments while preserving line/column positions."""
    chars = list(text)
    i = 0
    state = "normal"
    quote = ""
    while i < len(chars):
        current = chars[i]
        following = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "normal":
            if current in ('"', "'"):
                state = "string"
                quote = current
            elif current == "/" and following == "/":
                chars[i] = " "
                chars[i + 1] = " "
                i += 2
                state = "line_comment"
                continue
            elif current == "/" and following == "*":
                chars[i] = " "
                chars[i + 1] = " "
                i += 2
                state = "block_comment"
                continue
        elif state == "string":
            if current == "\\":
                i += 2
                continue
            if current == quote:
                state = "normal"
        elif state == "line_comment":
            if current == "\n":
                state = "normal"
            else:
                chars[i] = " "
        elif state == "block_comment":
            if current == "*" and following == "/":
                chars[i] = " "
                chars[i + 1] = " "
                i += 2
                state = "normal"
                continue
            if current != "\n":
                chars[i] = " "
        i += 1
    return "".join(chars)


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def match_evidence(path: Path, text: str, pattern: str, limit: int = 3) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    try:
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
    except re.error:
        matches = []
    for match in matches[:limit]:
        start = max(0, text.rfind("\n", 0, match.start()) + 1)
        end = text.find("\n", match.end())
        if end == -1:
            end = len(text)
        evidence.append({"path": path.as_posix(), "line": line_number(text, match.start()), "excerpt": text[start:end].strip()[:320]})
    return evidence


def contains_any(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


def literal_term_pattern(term: str) -> str:
    """Build a case-insensitive literal pattern without matching inside ASCII words."""
    stripped = term.strip()
    escaped = re.escape(stripped)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*[A-Za-z0-9]", stripped):
        return rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    return escaped


def extract_matches(text: str, patterns: Sequence[str]) -> list[str]:
    result: list[str] = []
    for pattern in patterns:
        result.extend(match.group(0) for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
    return result


def extract_ordinal(values: Iterable[str]) -> int | None:
    for value in values:
        match = re.search(r"(?:tier|pack|level|sku|档位)[\s._-]*0*(\d+)\b", value, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def parse_strings_file(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r'"([^"\\]+)"\s*=\s*"((?:\\.|[^"\\])*)"\s*;', text):
        result[match.group(1)] = bytes(match.group(2), "utf-8").decode("unicode_escape", errors="ignore")
    return result


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def aggregate_status(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return "NOT_VERIFIABLE"
    return max(
        (str(item.get("status", "NOT_VERIFIABLE")) for item in items),
        key=lambda status: STATUS_PRIORITY.get(status, STATUS_PRIORITY["NOT_VERIFIABLE"]),
    )


def aggregate_severity(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return "medium"
    return max(
        (str(item.get("severity", "info")) for item in items),
        key=lambda severity: SEVERITY_PRIORITY.get(severity, 0),
    )


def unique_text(values: Iterable[Any], separator: str = "；") -> str:
    result: list[str] = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in result:
            result.append(text)
    return separator.join(result)


def unique_evidence(items: Iterable[Mapping[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in items:
        normalised = dict(item)
        marker = (
            str(normalised.get("path", "")),
            int(normalised.get("line") or 1),
            str(normalised.get("excerpt", "")),
        )
        if marker in seen:
            continue
        seen.add(marker)
        result.append(normalised)
        if len(result) >= limit:
            break
    return result


class Auditor:
    def __init__(self, root: Path, policy: Mapping[str, Any] | None = None):
        self.source_scan_incomplete = False
        self.root = root.resolve()
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(policy)
        self.findings: list[dict[str, Any]] = []
        self.files: list[Path] = []
        self.source_texts: dict[Path, str] = {}
        self.all_texts: dict[Path, str] = {}
        self.json_values: list[tuple[Path, Any]] = []
        self.json_errors: list[tuple[Path, str]] = []
        self.plist_values: dict[Path, dict[str, Any]] = {}
        self.plist_sources: dict[Path, dict[str, int]] = {}
        self.iap_records: list[ProductRecord] = []
        self.code_products: list[CodeProduct] = []
        self.dynamic_code_products: list[dict[str, Any]] = []
        self.project_files: list[Path] = []
        self.storyboard_files: list[Path] = []

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            # Evidence is always presented relative to the audited project,
            # even when a future check receives a path outside the root.
            return Path(os.path.relpath(path, self.root)).as_posix()

    def add(
        self,
        rule_id: str,
        status: str,
        severity: str,
        title: str,
        expected: str = "",
        actual: str = "",
        evidence: list[dict[str, Any]] | None = None,
        manual_check: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        relative_evidence: list[dict[str, Any]] = []
        for item in evidence or []:
            normalised = dict(item)
            path_value = normalised.get("path")
            if isinstance(path_value, str) and Path(path_value).is_absolute():
                normalised["path"] = self.rel(Path(path_value))
            relative_evidence.append(normalised)

        finding = {
            "id": rule_id,
            "status": status,
            "severity": severity,
            "title": title,
            "expected": expected,
            "actual": actual,
            "evidence": relative_evidence,
            "manual_check": manual_check,
        }
        if details is not None:
            finding["details"] = details
        self.findings.append(finding)

    def add_group(
        self,
        rule_id: str,
        title: str,
        expected: str,
        details: Sequence[Mapping[str, Any]],
    ) -> None:
        copied = [dict(detail) for detail in details]
        status = aggregate_status(copied)
        severity = aggregate_severity(copied)
        actual = "；".join(
            f"{detail.get('label', rule_id)}：{detail.get('actual', '—')}"
            for detail in copied
        )
        evidence = unique_evidence(
            evidence_item
            for detail in copied
            for evidence_item in detail.get("evidence", [])
        )
        manual_check = unique_text(detail.get("manual_check") for detail in copied)
        self.add(
            rule_id,
            status,
            severity,
            title,
            expected=expected,
            actual=actual,
            evidence=evidence,
            manual_check=manual_check or None,
            details=copied,
        )

    def discover(self) -> None:
        ignored = set(IGNORED_DIRS) | {str(item) for item in self.policy.get("ignored_paths", [])}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in ignored for part in path.relative_to(self.root).parts):
                continue
            if path.suffix.lower() in TEXT_EXTENSIONS or path.name in {"project.pbxproj", "Info.plist"}:
                self.files.append(path)
                try:
                    try:
                        text = path.read_text(encoding="utf-8-sig")
                    except UnicodeDecodeError:
                        text = path.read_text(encoding="utf-8", errors="ignore")
                        if path.suffix.lower() in LEGAL_INPUT_EXTENSIONS:
                            self.source_scan_incomplete = True
                except OSError as error:
                    if path.suffix.lower() in LEGAL_INPUT_EXTENSIONS:
                        self.source_scan_incomplete = True
                    self.add("SCAN-001", "FAIL", "high", "无法读取项目文件", actual=f"{self.rel(path)}: {error}")
                    continue
                self.all_texts[path] = text
                if path.suffix.lower() in SOURCE_EXTENSIONS or path.name == "project.pbxproj":
                    self.source_texts[path] = text
                if path.name == "project.pbxproj":
                    self.project_files.append(path)
                if path.suffix.lower() == ".storyboard":
                    self.storyboard_files.append(path)

    def load_inputs(self) -> None:
        for path, text in self.all_texts.items():
            if path.suffix.lower() == ".json":
                try:
                    data = json.loads(text)
                    self.json_values.append((path, data))
                except json.JSONDecodeError as error:
                    if self._looks_like_iap_json(path, text):
                        repaired = re.sub(r",\s*([}\]])", r"\1", text)
                        try:
                            data = json.loads(repaired)
                            self.json_values.append((path, data))
                            self.json_errors.append((path, str(error)))
                        except json.JSONDecodeError:
                            self.json_errors.append((path, str(error)))
            elif path.suffix.lower() in {".plist", ".entitlements"}:
                try:
                    import plistlib

                    data = plistlib.loads(text.encode("utf-8"))
                    if isinstance(data, dict):
                        self.plist_values[path] = data
                        self.plist_sources[path] = self._plist_key_lines(text)
                except Exception as error:  # plistlib may raise multiple parser types
                    if path.name == "Info.plist" or "info" in path.name.lower():
                        self.add("PLIST-001", "FAIL", "high", "Info.plist 无法解析", actual=f"{self.rel(path)}: {error}")

            elif path.suffix.lower() == ".strings":
                values = parse_strings_file(text)
                if values:
                    self.plist_values.setdefault(path, {}).update(values)
                    self.plist_sources.setdefault(path, {}).update(self._plist_key_lines(text))

        for path, data in self.json_values:
            self.iap_records.extend(extract_product_records(path, data))
        self.code_products = self.extract_code_products()

    def _looks_like_iap_json(self, path: Path, text: str) -> bool:
        lowered = f"{path.name} {text[:3000]}".lower()
        return any(token in lowered for token in ("iap_products", "in_app_purchases", "submit_for_review", "price_usd", "product_id"))

    @staticmethod
    def _plist_key_lines(text: str) -> dict[str, int]:
        lines: dict[str, int] = {}
        for index, line in enumerate(text.splitlines(), start=1):
            match = re.search(r"<key>([^<]+)</key>|\"([^\"]+)\"\s*=", line)
            if match:
                lines[match.group(1) or match.group(2)] = index
        return lines

    def extract_code_products(self) -> list[CodeProduct]:
        products: list[CodeProduct] = []
        # A generic `id` is common in notifications, navigation and model data.
        # Only explicit product labels, configured IDs or local product context
        # establish that a string belongs to the IAP catalogue.
        id_pattern = re.compile(
            r"\b(?P<label>id|productID|productId|product_id)\b\s*[:=]\s*"
            r"(?P<quote>[\"'])(?P<value>(?:\\.|(?!(?P=quote))[^\\])*)(?P=quote)",
            flags=re.IGNORECASE,
        )
        price_pattern = re.compile(r"(?:configuredPrice|configured_price|price_usd|price)\s*[:=]\s*[\"']([^\"']+)[\"']", flags=re.IGNORECASE)
        name_pattern = re.compile(r"(?:title|name|referenceName|reference_name)\s*[:=]\s*[\"']([^\"']+)[\"']", flags=re.IGNORECASE)
        known_ids = {record.product_id for record in self.iap_records if record.product_id}
        self.dynamic_code_products = []
        for path, raw in self.source_texts.items():
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            clean, fields = identity_fields(raw)
            for field in fields:
                explicit = field.label.lower() != "id"
                if not explicit and field.has_explicit_identity:
                    continue
                match = id_pattern.match(clean, field.start)
                if match is None:
                    # Property forwarding in model initializers is not another
                    # catalogue entry (self.productId = productId).
                    member_assignment = bool(re.search(r"\.\s*$", clean[:field.start]))
                    if explicit and not member_assignment:
                        self.dynamic_code_products.append({
                            "path": self.rel(path),
                            "line": line_number(raw, field.start),
                            "excerpt": "商品 ID 为动态表达式，无法静态解析",
                        })
                    continue
                product_id = match.group("value").strip()
                context = field.context
                price_match = price_pattern.search(context)
                initializer = field.initializer
                product_initializer = bool(initializer and re.search(
                    r"(?:product|iap|sku|purchase)", initializer, re.IGNORECASE
                ))
                priced_product = bool(price_match and re.search(
                    r"\b(?:coins|credits|tokens|stamps|sku|referenceName|reference_name|configuredPrice|configured_price|price_usd)\s*[:=]",
                    context, re.IGNORECASE,
                ))
                explicit = match.group("label").lower() != "id"
                if not (explicit or product_id in known_ids or priced_product or product_initializer):
                    continue
                # Escaped/interpolated strings are expressions, not literal IDs.
                # Retain uncertainty instead of counting their prefix as a SKU.
                if "\\" in product_id or re.match(r"\s*\+", clean[match.end():]):
                    self.dynamic_code_products.append({
                        "path": self.rel(path),
                        "line": line_number(raw, match.start()),
                        "excerpt": "商品 ID 含插值、拼接或转义，无法静态解析",
                    })
                    continue
                names = [item.group(1).strip() for item in name_pattern.finditer(context)]
                products.append(
                    CodeProduct(
                        path=path,
                        line=line_number(raw, match.start()),
                        product_id=product_id,
                        price=parse_price(price_match.group(1)) if price_match else None,
                        names=list(dict.fromkeys(names)),
                    )
                )
        unique: dict[tuple[str, str, int], CodeProduct] = {}
        for product in products:
            unique[(str(product.path), product.product_id, product.line)] = product
        return list(unique.values())

    def run(self) -> "Auditor":
        self.discover()
        self.load_inputs()
        self.check_code_lines()
        self.check_iap()
        self.check_ab_and_restore()
        self.check_permissions()
        self.check_legal_links()
        self.check_ai_and_metadata()
        self.check_privacy_manifest()
        self.check_sensitive_terms()
        self.check_launch_and_name()
        return self

    def check_legal_links(self) -> None:
        entries = analyze_legal_links(
            self.root, self.source_texts, self.all_texts,
            scan_incomplete=self.source_scan_incomplete,
        )
        route_details: list[dict[str, Any]] = []
        for index, entry in enumerate(entries, 1):
            label = "隐私协议" if entry["kind"] == "privacy" else "用户协议"
            label += f" · 入口 {index}"
            evidence = entry.get("evidence", [])
            route_status = entry["status"]
            route_details.append({
                "id": "LEGAL-001", "label": label, "status": route_status,
                "severity": "high" if route_status == "FAIL" else "info" if route_status == "PASS" else "medium",
                "expected": "协议关联的页面或共用封装存在 WKWebView 加载调用",
                "actual": entry["actual"], "evidence": evidence,
                "manual_check": entry.get("manual_check"), "url": entry.get("url"),
            })
        self.add_group("LEGAL-001", "协议打开方式", "用户协议和隐私协议均通过端内 WKWebView 打开", route_details)

    def check_code_lines(self) -> None:
        finding = audit_code_lines(self.root, self.policy)
        rule_id = finding.pop("id")
        self.add(rule_id=rule_id, **finding)

    def check_iap(self) -> None:
        checks: list[dict[str, Any]] = []

        def add_check(
            rule_id: str,
            status: str,
            label: str,
            expected: str,
            actual: str,
            manual_check: str | None = None,
            evidence: list[dict[str, Any]] | None = None,
            severity: str | None = None,
        ) -> None:
            checks.append({
                "id": rule_id,
                "status": status,
                "severity": severity or ("high" if status == "FAIL" else "medium" if status in {"WARN", "NOT_VERIFIABLE"} else "info"),
                "label": label,
                "expected": expected,
                "actual": actual,
                "manual_check": manual_check,
                "evidence": evidence or [],
            })

        has_products = bool(self.iap_records or self.code_products)
        product_evidence = self._product_evidence()
        json_prices = {record.price for record in self.iap_records if record.price}
        code_prices = {product.price for product in self.code_products if product.price}
        all_prices = json_prices | code_prices
        required = {parse_price(price) or price for price in self.policy.get("required_prices_usd", [])}

        if not has_products:
            add_check(
                "IAP-001",
                "NOT_VERIFIABLE",
                "B 面必需内购档位",
                "包含 " + ", ".join(sorted(required, key=lambda item: Decimal(item))),
                "没有检测到可审计的 IAP 商品或价格",
                "确认项目是否使用远端配置、混淆字符串或未纳入当前目录的商品定义。",
            )
        else:
            if all_prices:
                missing = sorted(required - all_prices, key=lambda item: Decimal(item))
                add_check(
                    "IAP-001",
                    "FAIL" if missing else "PASS",
                    "B 面必需内购档位",
                    "包含 " + ", ".join(sorted(required, key=lambda item: Decimal(item))),
                    "缺少 " + ", ".join(missing) if missing else "六个默认档位均有证据；额外档位允许",
                    evidence=self._price_evidence(all_prices),
                    severity="blocker" if missing else "info",
                )
            else:
                add_check(
                    "IAP-001",
                    "NOT_VERIFIABLE",
                    "B 面必需内购档位",
                    "能从代码或 JSON 读取价格",
                    "没有可解析的价格字段",
                    "在项目配置、StoreKit 配置或 App Store Connect 中确认六个 B 面价格档位。",
                    evidence=product_evidence,
                )

        json_by_id = {record.product_id: record for record in self.iap_records if record.product_id}
        code_by_id: dict[str, CodeProduct] = {}
        for product in self.code_products:
            code_by_id.setdefault(product.product_id, product)
        if json_by_id and code_by_id:
            missing_in_code = len(set(json_by_id) - set(code_by_id))
            missing_in_json = len(set(code_by_id) - set(json_by_id))
            price_mismatches = sum(
                1
                for product_id in set(json_by_id) & set(code_by_id)
                if json_by_id[product_id].price
                and code_by_id[product_id].price
                and json_by_id[product_id].price != code_by_id[product_id].price
            )
            add_check(
                "IAP-002",
                "FAIL" if missing_in_json or price_mismatches or (missing_in_code and not self.dynamic_code_products) else "NOT_VERIFIABLE" if self.dynamic_code_products else "PASS",
                "代码与 JSON 内购配置一致性",
                "商品 ID 和价格映射一致",
                "；".join(
                    part
                    for part in (
                        f"代码 {len(code_by_id)} 项 / JSON {len(json_by_id)} 项",
                        f"代码缺失 {missing_in_code} 项" if missing_in_code else "",
                        f"JSON 缺失 {missing_in_json} 项" if missing_in_json else "",
                        f"价格不一致 {price_mismatches} 项" if price_mismatches else "",
                        "存在动态商品 ID，无法确认完整映射" if self.dynamic_code_products else "",
                    )
                    if part
                ),
                manual_check="在 StoreKit 或 App Store Connect 中核对动态商品 ID 和完整映射。" if self.dynamic_code_products else None,
                evidence=product_evidence + self.dynamic_code_products,
            )
        else:
            add_check(
                "IAP-002",
                "NOT_VERIFIABLE",
                "代码与 JSON 内购配置一致性",
                "代码和 JSON 两侧均有可识别商品",
                "缺少一侧或两侧的静态商品证据",
                "确认商品是否由远端配置、宏、混淆字符串或 StoreKit 配置文件提供。",
                evidence=product_evidence,
            )

        if not self.policy.get("require_submit_for_review", True):
            add_check(
                "IAP-003",
                "NOT_VERIFIABLE",
                "提交审核状态",
                "submit_for_review 默认要求为 true",
                "项目策略关闭了提交审核状态检查",
                "在 App Store Connect 中人工确认全部商品已提交审核。",
            )
        elif self.iap_records:
            total = len(self.iap_records)
            submitted = sum(record.submit_for_review is True for record in self.iap_records)
            rejected = sum(record.submit_for_review is False for record in self.iap_records)
            missing = sum(record.submit_for_review is None for record in self.iap_records)
            invalid = total - submitted - rejected - missing
            if rejected or invalid:
                status = "FAIL"
                severity = "blocker"
                manual_check = None
            elif missing:
                status = "NOT_VERIFIABLE"
                severity = "medium"
                manual_check = "补充 submit_for_review 布尔字段，或在 App Store Connect 中确认全部商品已提交审核。"
            else:
                status = "PASS"
                severity = "info"
                manual_check = None
            actual_parts = [f"{submitted}/{total} 项为 true"]
            if rejected:
                actual_parts.append(f"{rejected} 项为 false")
            if missing:
                actual_parts.append(f"{missing} 项缺少字段")
            if invalid:
                actual_parts.append(f"{invalid} 项不是布尔值")
            add_check(
                "IAP-003",
                status,
                "提交审核状态",
                "submit_for_review 默认要求为 true",
                "；".join(actual_parts),
                manual_check,
                evidence=product_evidence,
                severity=severity,
            )
        else:
            add_check(
                "IAP-003",
                "NOT_VERIFIABLE",
                "提交审核状态",
                "submit_for_review 默认要求为 true",
                "没有可解析的 IAP JSON 商品记录",
                "在 IAP 配置或 App Store Connect 中确认全部商品已提交审核。",
            )

        max_chars = int(self.policy.get("description_max_chars", 55))
        description_values = [description for record in self.iap_records for description in record.descriptions]
        if description_values:
            over_limit = sum(len(description) > max_chars for description in description_values)
            add_check(
                "IAP-005",
                "FAIL" if over_limit else "PASS",
                "description 长度",
                f"description 不超过 {max_chars} 个字符",
                f"{over_limit}/{len(description_values)} 条超过限制",
                evidence=product_evidence,
            )
        else:
            add_check(
                "IAP-005",
                "NOT_VERIFIABLE",
                "description 长度",
                f"description 不超过 {max_chars} 个字符",
                "没有可解析的 IAP description 文案",
                "在本地 IAP 配置或 App Store Connect 中核对 description 长度。",
            )

        product_ids = list(dict.fromkeys(
            [record.product_id for record in self.iap_records if record.product_id]
            + [product.product_id for product in self.code_products if product.product_id]
        ))
        if product_ids:
            lowercase_total = sum(product_id == product_id.lower() for product_id in product_ids)
            total_ids = len(product_ids)
            add_check(
                "IAP-006",
                "FAIL" if lowercase_total != total_ids else "NOT_VERIFIABLE" if self.dynamic_code_products else "PASS",
                "product ID 大小写",
                "所有 product ID 全小写",
                f"{lowercase_total}/{total_ids} 项静态 ID 全小写" + ("；动态商品 ID 大小写无法确认" if self.dynamic_code_products else ""),
                manual_check="在 StoreKit 或 App Store Connect 中核对动态商品 ID 的大小写。" if self.dynamic_code_products else None,
                evidence=product_evidence,
            )
        else:
            add_check(
                "IAP-006",
                "NOT_VERIFIABLE",
                "product ID 大小写",
                "所有 product ID 全小写",
                "没有可解析的 product ID",
                "检查远端配置、混淆字符串或 App Store Connect 商品 ID。",
            )

        # Preserve each JSON catalogue's original order; sorting first would hide errors.
        catalogues: dict[tuple[Path, str], list[ProductRecord]] = {}
        for record in self.iap_records:
            container = re.sub(r"\[\d+\]$", "", record.pointer)
            catalogues.setdefault((record.path, container), []).append(record)
        baseline = [parse_price(value) for value in self.policy.get("required_prices_usd", [])]
        minimum = min((Decimal(value) for value in baseline if value is not None), default=None)
        invalid_order = []
        missing_prices = False
        for (path, container), records in catalogues.items():
            prices = [Decimal(record.price) for record in records if record.price is not None]
            missing_prices |= len(prices) != len(records)
            # Missing prices cannot establish a complete sequence, but known inversions still fail.
            wrong_start = bool(records and records[0].price is not None and minimum is not None
                               and Decimal(records[0].price) != minimum)
            if wrong_start or any(left >= right for left, right in zip(prices, prices[1:])):
                invalid_order.append(f"{self.rel(path)} {container}")
        start_label = f"从 ${minimum:.2f} 起" if minimum is not None else ""
        add_check(
            "IAP-007",
            "FAIL" if invalid_order else "NOT_VERIFIABLE" if missing_prices or not catalogues else "PASS",
            "档位价格顺序",
            f"每份商品列表{start_label}按价格递增；不要求名称或 ID 包含序号",
            "价格顺序不符合要求：" + "、".join(invalid_order) if invalid_order else
            "存在缺失或无法解析的商品价格" if missing_prices else
            f"{len(catalogues)} 份商品列表{start_label}按价格递增" if catalogues else "没有可解析的 IAP JSON 商品列表",
            "核对商品列表原有顺序和每档美元价格；购买成功、交易验证及发放需逐档沙盒验证。"
            if missing_prices or not catalogues or invalid_order else None,
            evidence=product_evidence,
        )

        iap_json_present = any(
            path.suffix.lower() == ".json" and self._looks_like_iap_json(path, text)
            for path, text in self.all_texts.items()
        )
        if self.json_errors:
            add_check(
                "IAP-008",
                "FAIL",
                "IAP JSON 解析",
                "所有 IAP 配置为有效 JSON",
                f"{len(self.json_errors)} 个 IAP 配置文件无法按标准 JSON 解析",
                evidence=[{"path": self.rel(path), "line": 1, "excerpt": error[:320]} for path, error in self.json_errors],
                severity="blocker",
            )
        elif iap_json_present:
            add_check(
                "IAP-008",
                "PASS",
                "IAP JSON 解析",
                "所有 IAP 配置为有效 JSON",
                "检测到的 IAP JSON 均可按标准 JSON 解析",
                evidence=[{"path": self.rel(record.path), "line": 1, "excerpt": "有效 IAP JSON"} for record in self.iap_records[:5]],
            )
        else:
            add_check(
                "IAP-008",
                "NOT_VERIFIABLE",
                "IAP JSON 解析",
                "所有 IAP 配置为有效 JSON",
                "未发现可识别的 IAP JSON 配置",
                "确认商品是否完全由代码、远端配置或 App Store Connect 提供。",
            )

        check_order = {rule_id: index for index, rule_id in enumerate(("IAP-001", "IAP-002", "IAP-003", "IAP-005", "IAP-006", "IAP-007", "IAP-008"))}
        checks.sort(key=lambda check: check_order[check["id"]])
        self.add_group(
            "IAP-SUMMARY",
            "内购项统一检查",
            "统一检查价格档位、代码/JSON 一致性、提交审核、product ID、description、档位命名和 JSON 可解析性",
            checks,
        )

    def _price_evidence(self, prices: Iterable[str]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        wanted = set(prices)
        for path, text in self.all_texts.items():
            for price in wanted:
                if price in text or price.replace(".", ",") in text:
                    result.append({"path": self.rel(path), "line": line_number(text, text.find(price)), "excerpt": price})
                    if len(result) >= 8:
                        return result
        return result

    def _product_evidence(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = list(self.dynamic_code_products)
        for record in self.iap_records[:5]:
            result.append({"path": self.rel(record.path), "line": 1, "excerpt": "检测到 IAP 商品配置"})
        for product in self.code_products[:5]:
            result.append({"path": self.rel(product.path), "line": product.line, "excerpt": "检测到 IAP 商品代码"})
        return unique_evidence(result, limit=8)

    def _iap_summary_evidence(self) -> list[dict[str, Any]]:
        paths = sorted({record.path for record in self.iap_records} | {product.path for product in self.code_products}, key=lambda path: path.as_posix())
        return [
            {"path": self.rel(path), "line": 1, "excerpt": "检测到 IAP 配置或代码"}
            for path in paths[:8]
        ]

    def check_ab_and_restore(self) -> None:
        delegate_hits: list[dict[str, Any]] = []
        assignment_hits: list[dict[str, Any]] = []
        restore_hits: list[dict[str, Any]] = []
        delegate_pattern = r"UNUserNotificationCenterDelegate"
        assignment_pattern = r"UNUserNotificationCenter\s*\.\s*current\s*\(\s*\)\s*\.\s*delegate\s*="
        for path, raw in self.source_texts.items():
            clean = mask_comments(raw)
            delegate_hits.extend({**item, "path": self.rel(path)} for item in match_evidence(path, clean, delegate_pattern))
            assignment_hits.extend({**item, "path": self.rel(path)} for item in match_evidence(path, clean, assignment_pattern))
        for path, raw in self.all_texts.items():
            restore_hits.extend({**item, "path": self.rel(path)} for item in detect_restore(path, raw))
        if self.policy.get("forbid_notification_delegate", True):
            evidence = delegate_hits[:3] + assignment_hits[:3]
            self.add("AB-001", "FAIL" if evidence else "PASS", "blocker" if evidence else "info", "A 面未抢占通知代理", expected="不存在通知代理协议或代理赋值", actual="发现通知代理实现/赋值" if evidence else "未发现通知代理实现或赋值", evidence=evidence)
        else:
            self.add("AB-001", "NOT_VERIFIABLE", "medium", "A 面通知代理检查已关闭", expected="不存在通知代理协议或代理赋值", actual="项目策略 forbid_notification_delegate=false", manual_check="人工确认 A 面没有覆盖 B 面需要的通知代理。")
        if self.policy.get("forbid_restore", True):
            self.add("IAP-009", "FAIL" if restore_hits else "PASS", "high" if restore_hits else "info", "A 面不提供恢复购买入口", expected="不存在 restore API 或恢复购买按钮", actual="发现恢复购买代码或操作入口" if restore_hits else "未发现恢复购买入口", evidence=restore_hits[:6])
        else:
            self.add("IAP-009", "NOT_VERIFIABLE", "medium", "恢复购买入口检查已关闭", expected="不存在 restore API 或恢复购买按钮", actual="项目策略 forbid_restore=false", manual_check="人工确认恢复购买入口符合当前商品类型和审核策略。")

    def check_permissions(self) -> None:
        permission_config_details: list[dict[str, Any]] = []
        permission_copy_details: list[dict[str, Any]] = []
        att_config_details: list[dict[str, Any]] = []
        att_copy_details: list[dict[str, Any]] = []

        for resource, spec in PURPOSE_SPECS.items():
            declarations = self._xcode_permission_declarations(spec)
            used_paths = self._permission_used_paths(spec)
            used = bool(used_paths)

            if resource == "push":
                config_evidence = self._push_config_evidence()
                configured = bool(config_evidence)
            else:
                config_evidence = [
                    {"path": self.rel(path), "line": line, "excerpt": f"{key} = {value}"}
                    for path, key, line, value in declarations
                ]
                configured = bool(declarations)

            config_rule = "ATT-001" if resource == "tracking" else "PERM-001"
            copy_rule = "ATT-002" if resource == "tracking" else "PERM-002"
            copy_expectation = (
                "文案非空且不是默认个性化推荐或个性化内容模板"
                if resource == "tracking" else "说明权限资源和用途动作"
            )
            label = spec["label"]
            if resource == "push":
                permission_config_details.append({
                    "id": config_rule,
                    "status": "PASS" if configured or not used else "FAIL",
                    "severity": "info" if configured or not used else "high",
                    "label": label,
                    "expected": self._permission_config_expectation(resource, spec),
                    "actual": (
                        "找到 Push 权限配置；无需检查用途文案或运行时行为"
                        if configured else
                        "检测到 Push API 使用，但未找到对应 Xcode 配置"
                        if used else
                        "未检测到 Push API 且未配置，无需配置"
                    ),
                    "evidence": config_evidence[:5] if configured else self._permission_usage_evidence(spec, used_paths),
                    "manual_check": None,
                })
                continue
            if used and not configured:
                config_detail = {
                    "id": config_rule,
                    "status": "FAIL",
                    "severity": "high",
                    "label": label,
                    "expected": self._permission_config_expectation(resource, spec),
                    "actual": "检测到权限 API 使用，但未找到对应 Xcode 配置",
                    "evidence": self._permission_usage_evidence(spec, used_paths),
                    "manual_check": None,
                }
                copy_detail = {
                    "id": copy_rule,
                    "status": "NOT_VERIFIABLE",
                    "severity": "medium",
                    "label": label,
                    "expected": copy_expectation,
                    "actual": "缺少权限配置，无法检查对应用途文案",
                    "evidence": [],
                    "manual_check": "补充 Xcode 权限配置后重新检查用途文案。",
                }
            elif not used and not configured:
                config_detail = {
                    "id": config_rule,
                    "status": "PASS",
                    "severity": "info",
                    "label": label,
                    "expected": self._permission_config_expectation(resource, spec),
                    "actual": "未检测到权限 API 且未配置，无需配置",
                    "evidence": [],
                    "manual_check": None,
                }
                copy_detail = {
                    "id": copy_rule,
                    "status": "PASS",
                    "severity": "info",
                    "label": label,
                    "expected": "使用该权限时提供合格用途文案",
                    "actual": "未检测到权限 API 且未配置，无需用途文案",
                    "evidence": [],
                    "manual_check": None,
                }
            else:
                config_detail = {
                    "id": config_rule,
                    "status": "PASS",
                    "severity": "info",
                    "label": label,
                    "expected": self._permission_config_expectation(resource, spec),
                    "actual": "找到对应 Xcode 配置" + ("并检测到 API 使用" if used else "；未检测到 API 使用但声明本身不判失败"),
                    "evidence": config_evidence[:5],
                    "manual_check": None,
                }

                quality_results = [
                    (path, key, line, value, *self._purpose_quality(value, spec))
                    for path, key, line, value in declarations
                ]
                passing = sum(bool(item[4]) for item in quality_results)
                total = len(quality_results)
                copy_detail = {
                    "id": copy_rule,
                    "status": "PASS" if passing == total else "FAIL",
                    "severity": "info" if passing == total else "medium",
                    "label": label,
                    "expected": copy_expectation,
                    "actual": f"{passing}/{total} 条用途文案通过；" + unique_text(item[5] for item in quality_results),
                    "evidence": [
                        {"path": self.rel(path), "line": line, "excerpt": value[:320]}
                        for path, _key, line, value, _quality, _reason in quality_results
                    ],
                    "manual_check": None if passing == total else "修改未通过的用途文案后重新扫描。",
                }

            if resource == "tracking":
                att_config_details.append(config_detail)
                att_copy_details.append(copy_detail)
            else:
                permission_config_details.append(config_detail)
                permission_copy_details.append(copy_detail)

        self.add_group(
            "PERM-001",
            "权限 Xcode 配置",
            "相机、相册、麦克风或 Push API 使用时存在对应 Xcode 配置",
            permission_config_details,
        )
        self.add_group(
            "PERM-002",
            "权限用途文案",
            "已配置相机、相册、麦克风权限的用途文案说明资源对象和用途动作；Push 仅检查配置",
            permission_copy_details,
        )
        self.add_group(
            "ATT-001",
            "ATT Xcode 配置",
            "发现 ATT API 或追踪标识时存在 NSUserTrackingUsageDescription",
            att_config_details,
        )
        self.add_group(
            "ATT-002",
            "ATT 用途文案",
            "NSUserTrackingUsageDescription 非空且不是默认个性化推荐或个性化内容模板",
            att_copy_details,
        )

    def _xcode_permission_declarations(self, spec: Mapping[str, Any]) -> list[tuple[Path, str, int, str]]:
        declarations: list[tuple[Path, str, int, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for path, values in self.plist_values.items():
            for key in spec["keys"]:
                if key not in values:
                    continue
                line = self.plist_sources.get(path, {}).get(key, 1)
                value = as_string(values.get(key)) or ""
                marker = (str(path), key, value)
                if marker not in seen:
                    declarations.append((path, key, line, value))
                    seen.add(marker)

        for path, raw in self.source_texts.items():
            if path.name != "project.pbxproj" and path.suffix.lower() != ".xcconfig":
                continue
            clean = mask_comments(raw)
            for key in spec["keys"]:
                pattern = rf"\bINFOPLIST_KEY_{re.escape(key)}\s*=\s*([^;\n]+)"
                for match in re.finditer(pattern, clean, flags=re.IGNORECASE):
                    value = match.group(1).strip().strip('"')
                    marker = (str(path), key, value)
                    if marker in seen:
                        continue
                    declarations.append((path, key, line_number(raw, match.start()), value))
                    seen.add(marker)
        return declarations

    def _push_config_evidence(self) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        seen: set[tuple[str, int, str]] = set()
        for path, values in self.plist_values.items():
            if "aps-environment" not in values:
                continue
            line = self.plist_sources.get(path, {}).get("aps-environment", 1)
            value = as_string(values.get("aps-environment")) or ""
            if value not in {"development", "production"}:
                continue
            item = {"path": self.rel(path), "line": line, "excerpt": f"aps-environment = {value}"}
            marker = (item["path"], line, item["excerpt"])
            if marker not in seen:
                evidence.append(item)
                seen.add(marker)

        for path, raw in self.all_texts.items():
            if path.suffix.lower() not in {".entitlements", ".xcconfig"} and path.name != "project.pbxproj":
                continue
            clean = mask_comments(raw)
            patterns = (
                r'(?<![\w-])"?aps-environment"?\s*=\s*(?:"(?:development|production)"|(?:development|production))\s*(?=;|\n|$)',
                r"com\.apple\.Push\s*=\s*\{\s*enabled\s*=\s*1\s*;",
            )
            for pattern in patterns:
                for item in match_evidence(path, clean, pattern):
                    item["path"] = self.rel(path)
                    marker = (item["path"], item["line"], item["excerpt"])
                    if marker not in seen:
                        evidence.append(item)
                        seen.add(marker)
        return evidence

    def _permission_used_paths(self, spec: Mapping[str, Any]) -> list[Path]:
        return [
            path
            for path, raw in self.source_texts.items()
            if contains_any(mask_comments(raw), spec["patterns"])
        ]

    def _permission_usage_evidence(self, spec: Mapping[str, Any], paths: Sequence[Path]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for path in paths[:3]:
            raw = self.source_texts[path]
            clean = mask_comments(raw)
            for pattern in spec["patterns"]:
                hits = match_evidence(path, clean, pattern, limit=1)
                if hits:
                    hits[0]["path"] = self.rel(path)
                    evidence.append(hits[0])
                    break
            else:
                evidence.append({"path": self.rel(path), "line": 1, "excerpt": "检测到权限 API"})
        return evidence

    def _permission_config_expectation(self, resource: str, spec: Mapping[str, Any]) -> str:
        if resource == "push":
            return "Xcode 的 entitlements 存在 aps-environment，或 Push Notifications capability 已启用"
        return "Xcode 的 Info.plist/Build Settings 存在 " + "、".join(spec["keys"])

    def _purpose_quality(self, value: str, spec: Mapping[str, Any]) -> tuple[bool, str]:
        if "NSUserTrackingUsageDescription" in spec["keys"]:
            if not value.strip():
                return False, "ATT 用途文案为空"
            # This project policy only rejects explicit recommendation/content
            # templates. Personalized ads alone are not a recommendation template.
            template_patterns = (
                r"个性化\s*(?:的\s*)?(?:推荐|内容)",
                r"(?:推荐|内容)\s*个性化",
                r"\bpersonali[sz]ed[\s-]+(?:recommendations?|content)\b",
                r"\bpersonali[sz](?:e|ing|ation)[\s-]+(?:your[\s-]+)?(?:recommendations?|content)\b",
            )
            if contains_any(value, template_patterns):
                return False, "命中默认个性化推荐或个性化内容模板"
            return True, "文案非空且未命中默认个性化推荐或个性化内容模板"
        minimum = int(self.policy.get("purpose_min_chars", 20))
        lowered = value.lower()
        if len(value.strip()) < minimum:
            return False, f"只有 {len(value.strip())} 个字符，少于 {minimum}"
        has_object = any(item.lower() in lowered for item in spec["objects"])
        has_action = any(item.lower() in lowered for item in spec["actions"])
        if has_object and has_action:
            return True, "包含资源/数据对象和使用动作"
        missing = []
        if not has_object:
            missing.append("资源/数据对象")
        if not has_action:
            missing.append("使用动作")
        return False, "缺少 " + "、".join(missing)

    def check_ai_and_metadata(self) -> None:
        source_text = "\n".join(mask_comments(raw) for raw in self.source_texts.values())
        ai_evidence: list[dict[str, Any]] = []
        personal_evidence: list[dict[str, Any]] = []
        for path, raw in self.source_texts.items():
            clean = mask_comments(raw)
            for pattern in AI_PATTERNS:
                ai_evidence.extend({**item, "path": self.rel(path)} for item in match_evidence(path, clean, pattern))
            for pattern in PERSONAL_DATA_PATTERNS:
                personal_evidence.extend({**item, "path": self.rel(path)} for item in match_evidence(path, clean, pattern))
        privacy_paths = [path for path in self.all_texts if any(token in path.name.lower() for token in ("privacy", "policy", "terms", "agreement"))]
        privacy_text = "\n".join(self.all_texts[path] for path in privacy_paths)
        if ai_evidence and personal_evidence:
            has_data = contains_any(privacy_text, PERSONAL_DATA_PATTERNS)
            has_recipient = contains_any(privacy_text, (r"third[- ]party", r"third party", r"openai", r"anthropic", r"ai service", r"第三方", r"人工智能"))
            has_consent = contains_any(privacy_text + "\n" + source_text, (r"consent", r"permission", r"agree", r"allow", r"before sending", r"同意", r"许可", r"授权"))
            missing = [label for present, label in ((has_data, "数据类别"), (has_recipient, "接收方"), (has_consent, "发送前同意")) if not present]
            self.add("PRIV-001", "FAIL" if missing else "NOT_VERIFIABLE", "high", "第三方 AI 数据共享说明", expected="披露数据、接收方并在发送前取得许可", actual="缺少 " + "、".join(missing) if missing else "发现 AI 与个人数据证据，文案覆盖但实际发送仍需验证", evidence=(ai_evidence[:3] + personal_evidence[:3]), manual_check="使用真机或网络代理确认发送前确实阻塞在用户同意之后，并核对隐私政策 URL 与提交版本一致。")
        elif ai_evidence:
            self.add("PRIV-001", "NOT_VERIFIABLE", "medium", "发现第三方 AI 代码但未确认个人数据范围", expected="明确 AI 接收数据和用途", actual="发现 AI SDK/endpoint，未静态确认个人数据是否发送", evidence=ai_evidence[:5], manual_check="检查请求 body、图片/音频上传和隐私政策。")
        else:
            self.add(
                "PRIV-001",
                "PASS",
                "info",
                "第三方 AI 数据共享",
                expected="披露数据、接收方并在发送前取得许可",
                actual=f"扫描 {len(self.source_texts)} 个源码/工程文件，未发现第三方 AI endpoint 或 SDK 证据",
            )

        metadata_paths = [path for path in self.all_texts if path.suffix.lower() in {".txt", ".md", ".html", ".htm", ".json", ".strings"}]
        metadata_text = "\n".join(self.all_texts[path] for path in metadata_paths)
        self.check_app_description()

        download_price = self._download_price()
        says_free = contains_any(metadata_text, (r"\bfree\b", r"免费"))
        if says_free and download_price is None:
            self.add("META-002", "NOT_VERIFIABLE", "medium", "免费/价格元数据无法对照", expected="有下载价格来源", actual="元数据出现 free/免费但未找到下载价格配置", manual_check="在 App Store Connect 检查 App 下载价格与截图、描述中的免费文案。")
        elif says_free and download_price not in {"free", "0.00", "0"}:
            self.add("META-002", "FAIL", "high", "免费文案与下载价格冲突", expected="下载价格为免费时才能使用 free/免费", actual=f"下载价格为 {download_price}")
        elif download_price is not None:
            self.add("META-002", "PASS", "info", "免费/价格元数据", expected="元数据与下载价格一致", actual=f"下载价格 {download_price}")
        else:
            self.add(
                "META-002",
                "NOT_VERIFIABLE",
                "medium",
                "免费/价格元数据无法对照",
                expected="本地下载价格与免费声明一致",
                actual="未找到可解析的下载价格，也未发现明确 free/免费声明",
                manual_check="在 App Store Connect 核对 App 下载价格以及截图和描述中的免费文案。",
            )

        iap_summary = next((finding for finding in self.findings if finding.get("id") == "IAP-SUMMARY"), None)
        submit_detail = next(
            (
                detail
                for detail in (iap_summary or {}).get("details", [])
                if detail.get("id") == "IAP-003"
            ),
            None,
        )
        if submit_detail:
            self.add(
                "META-003",
                str(submit_detail["status"]),
                str(submit_detail.get("severity", "medium")),
                "IAP 提交状态",
                expected=str(submit_detail.get("expected", "全部商品已提交审核")),
                actual=str(submit_detail.get("actual", "—")),
                evidence=submit_detail.get("evidence", []),
                manual_check=submit_detail.get("manual_check"),
            )
        else:
            self.add(
                "META-003",
                "NOT_VERIFIABLE",
                "medium",
                "IAP 提交状态",
                expected="全部 IAP 商品已提交审核",
                actual="没有可复用的 IAP-003 静态结论",
                manual_check="在 App Store Connect 核对每个 IAP 商品的提交状态。",
            )

    def check_privacy_manifest(self) -> None:
        if not self.policy.get("forbid_privacy_manifest", True):
            self.add(
                "PRIV-002",
                "NOT_VERIFIABLE",
                "medium",
                "隐私清单文件检查已关闭",
                expected="项目中不存在 PrivacyInfo.xcprivacy（A 面无需该文件）",
                actual="项目策略 forbid_privacy_manifest=false",
                manual_check="人工确认当前 A 面策略是否允许包含 PrivacyInfo.xcprivacy。",
            )
            return

        manifests = [path for path in self.files if path.name.lower() == "privacyinfo.xcprivacy"]
        evidence = [
            {"path": self.rel(path), "line": 1, "excerpt": path.name}
            for path in manifests
        ]
        self.add(
            "PRIV-002",
            "FAIL" if manifests else "PASS",
            "high" if manifests else "info",
            "A 面不包含隐私清单文件",
            expected="项目中不存在 PrivacyInfo.xcprivacy（A 面无需该文件）",
            actual=f"发现 {len(manifests)} 个 PrivacyInfo.xcprivacy" if manifests else "未发现 PrivacyInfo.xcprivacy",
            evidence=evidence,
        )

    def check_sensitive_terms(self) -> None:
        configured = self.policy.get("sensitive_terms", DEFAULT_POLICY["sensitive_terms"])
        if not isinstance(configured, Mapping):
            self.add(
                "SENSITIVE-001",
                "FAIL",
                "high",
                "敏感词检查策略无效",
                expected="sensitive_terms 为 category 到词语数组的 JSON object",
                actual="策略中的 sensitive_terms 不是 JSON object",
                manual_check="修正项目策略 JSON 后重新执行审计。",
            )
            return

        if not configured:
            self.add(
                "SENSITIVE-001",
                "NOT_VERIFIABLE",
                "medium",
                "敏感词检查已关闭",
                expected="使用有效词库扫描源码、配置、元数据和隐私文本",
                actual="项目策略将 sensitive_terms 设为空 object",
                manual_check="人工检查敏感文案，或配置需要扫描的敏感词类别后重新执行。",
            )
            return

        category_hits: list[dict[str, Any]] = []
        for category, raw_terms in configured.items():
            if isinstance(raw_terms, str):
                terms = [raw_terms]
            elif isinstance(raw_terms, Sequence) and not isinstance(raw_terms, (bytes, bytearray)):
                terms = [str(term).strip() for term in raw_terms if str(term).strip()]
            else:
                continue

            matched_terms: list[str] = []
            evidence: list[dict[str, Any]] = []
            for path, raw in self.all_texts.items():
                text = mask_comments(raw) if path.suffix.lower() in SOURCE_EXTENSIONS or path.name == "project.pbxproj" else raw
                for term in terms:
                    pattern = literal_term_pattern(term)
                    if not re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
                        continue
                    if term not in matched_terms:
                        matched_terms.append(term)
                    for item in match_evidence(path, text, pattern, limit=1):
                        evidence.append({**item, "path": self.rel(path), "category": str(category), "term": term})

            if matched_terms:
                category_hits.append({
                    "category": str(category),
                    "terms": matched_terms,
                    "evidence": evidence[:8],
                    "file_count": len({item["path"] for item in evidence}),
                })

        if not category_hits:
            self.add(
                "SENSITIVE-001",
                "PASS",
                "info",
                "敏感词检查",
                expected="源码、配置、权限文案、商店元数据和隐私文本不出现配置的敏感词",
                actual=f"扫描 {len(self.all_texts)} 个文本文件，未命中 {len(configured)} 个敏感词类别",
                manual_check="静态扫描不覆盖远端配置、服务端下发文案、截图文字和运行时拼接内容。",
            )
            return

        category_summary = "；".join(
            f"{item['category']}：{', '.join(item['terms'])}（{item['file_count']} 个文件）"
            for item in category_hits
        )
        evidence = [item for category in category_hits for item in category["evidence"]][:20]
        self.add(
            "SENSITIVE-001",
            "FAIL",
            "high",
            "敏感词检查",
            expected="源码、配置、权限文案、商店元数据和隐私文本不出现配置的敏感词",
            actual=f"命中 {len(category_hits)} 个类别：{category_summary}",
            evidence=evidence,
            manual_check="逐项确认命中文案是否会出现在 App、App Store 元数据、截图或审核备注中；删除或改写不必要的敏感词，并同步检查远端配置、服务端文案和运行时下发内容。",
        )

    def check_app_description(self) -> None:
        """Accept only explicit app-description fields or conventional store-description files."""
        descriptions: list[tuple[Path, str, str]] = []
        missing: list[str] = []
        excluded = re.compile(r"(?:^|[./_\[ -])(?:iap|iap_products|products|in_app_purchases|review|review_notes|review_information|app_review_information|privacy|policy|terms|agreement|readme)(?:$|[./_\[ -])", re.I)
        app_context = re.compile(r"(?:^|\.)(?:app|app_info|app_store|metadata|version|versions|app_store_version|app_store_versions|version_localizations|app_store_version_localizations|localizations)(?:$|\.|\[)", re.I)
        for path, data in self.json_values:
            product_pointers = [record.pointer for record in self.iap_records if record.path == path]
            for pointer, mapping in iter_dicts(data):
                if excluded.search(pointer) or any(pointer == prefix or pointer.startswith(prefix + ".") or pointer.startswith(prefix + "[") for prefix in product_pointers):
                    continue
                explicit_context = bool(app_context.search(pointer)) or (pointer == "$" and any(token in path.stem.lower() for token in ("app_store", "metadata")))
                if explicit_context and "description" in mapping:
                    value = mapping["description"]
                    descriptions.append((path, pointer + ".description", value if isinstance(value, str) else ""))
                if "description_file" in mapping:
                    value = mapping["description_file"]
                    if not isinstance(value, str) or not value.strip():
                        missing.append(f"{self.rel(path)} {pointer}.description_file")
                        continue
                    target = (path.parent / value).resolve()
                    if excluded.search(self.rel(target)):
                        missing.append(f"{self.rel(path)}：描述引用指向非应用描述文件")
                        continue
                    try:
                        descriptions.append((target, "description_file", target.read_text(encoding="utf-8")))
                    except (OSError, UnicodeError):
                        missing.append(self.rel(target))
        for path, text in self.all_texts.items():
            relative = self.rel(path)
            if excluded.search(relative) or path.suffix.lower() not in {".txt", ".md", ".html", ".htm"}:
                continue
            stem = path.stem.lower()
            in_metadata = any(part.lower() in {"metadata", "descriptions", "app_store", "appstore"} for part in Path(relative).parts[:-1])
            locale_name = re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", stem)
            if stem in {"metadata", "description", "app_description", "app-store-description", "app_store_description"} or (in_metadata and locale_name):
                descriptions.append((path, "应用描述文件", text))
        # A referenced file may also be found by conventional path discovery.
        # Count the document once, but retain separate inline JSON fields.
        unique_descriptions: dict[tuple[Path, str], tuple[Path, str, str]] = {}
        for path, pointer, text in descriptions:
            identity = pointer if pointer.startswith("$") else "file"
            unique_descriptions.setdefault((path.resolve(), identity), (path, pointer, text))
        descriptions = list(unique_descriptions.values())
        nonempty = [(path, pointer, text) for path, pointer, text in descriptions if text.strip()]
        status = "PASS" if nonempty else "FAIL" if descriptions else "NOT_VERIFIABLE"
        selected = nonempty or descriptions
        evidence = []
        for path, pointer, text in selected:
            raw = self.all_texts.get(path, text)
            needle = json.dumps(text, ensure_ascii=False) if path.suffix == ".json" else text
            offset = raw.find(needle)
            evidence.append({"path": self.rel(path), "line": line_number(raw, max(offset, 0)), "excerpt": f"{pointer}: {text.strip()[:240]}"})
        self.add(
            "META-001", status, "info" if status == "PASS" else "high" if status == "FAIL" else "medium",
            "商店应用描述", expected="存在非空应用描述文案；不要求购买方式或购买用途说明",
            actual=f"检测到 {len(nonempty)} 份非空应用描述" if nonempty else "应用描述文案为空" if descriptions else
            "未找到可确认的应用描述" + ("；无法读取描述引用：" + "、".join(missing) if missing else ""),
            evidence=unique_evidence(evidence),
            manual_check=None if nonempty else "提供应用描述正文或有效的 description_file，核对 App Store Connect 中的应用描述。",
        )

    def _download_price(self) -> str | None:
        for _, data in self.json_values:
            for pointer, mapping in iter_dicts(data):
                if "download_price" in mapping:
                    return as_string(mapping["download_price"])
                app = mapping.get("app")
                if isinstance(app, dict) and "download_price" in app:
                    return as_string(app["download_price"])
        return None

    def check_launch_and_name(self) -> None:
        launch_names: list[tuple[Path, str, int]] = []
        for path, values in self.plist_values.items():
            if "UILaunchStoryboardName" in values:
                launch_names.append((path, as_string(values["UILaunchStoryboardName"]) or "", self.plist_sources.get(path, {}).get("UILaunchStoryboardName", 1)))
        for path in self.project_files:
            raw = self.source_texts[path]
            for match in re.finditer(r"INFOPLIST_KEY_UILaunchStoryboardName\s*=\s*([^;]+);", raw):
                launch_names.append((path, match.group(1).strip().strip('"'), line_number(raw, match.start())))
        if not launch_names:
            self.add("IOS-001", "FAIL", "blocker", "缺少 LaunchScreen 配置", expected="target 配置 UILaunchStoryboardName", actual="没有找到 Info.plist 或构建设置中的启动 storyboard", manual_check="检查每个可构建 target 的 Info.plist 和 Build Settings。")
        else:
            storyboard_by_stem: dict[str, list[Path]] = {}
            for storyboard in self.storyboard_files:
                storyboard_by_stem.setdefault(storyboard.stem.lower(), []).append(storyboard)
            has_synced_project = any("PBXFileSystemSynchronizedRootGroup" in self.source_texts[p] for p in self.project_files)
            for path, value, line in launch_names:
                if "$" in value:
                    self.add("IOS-001", "NOT_VERIFIABLE", "medium", "LaunchScreen 名称包含未解析变量", expected="可解析的启动 storyboard 名称", actual=f"配置 {value}，无法静态解析", evidence=[{"path": self.rel(path), "line": line, "excerpt": value}], manual_check="确认对应 target 最终构建设置中的 UILaunchStoryboardName。")
                    continue
                name = value.removesuffix(".storyboard")
                candidates = storyboard_by_stem.get(name.lower(), [])
                if candidates:
                    evaluations = []
                    for candidate in candidates:
                        membership = []
                        for project in self.project_files:
                            raw = self.source_texts[project]
                            if has_synced_project:
                                included, reason = evaluate_membership(project, raw, candidate, path)
                            else:
                                included = candidate.name in raw
                                reason = "检测到传统 pbxproj 文件引用" if included else "未确认传统 pbxproj 资源引用"
                            membership.append((project, included, reason))
                        evaluations.append((candidate, membership))
                    candidate, membership = next((item for item in evaluations if any(included for _, included, _ in item[1])), evaluations[0])
                    project_mentions = any(included for _, included, _ in membership)
                    status = "PASS" if project_mentions or not self.project_files else "NOT_VERIFIABLE"
                    reasons = "；".join(dict.fromkeys(reason for _, included, reason in membership if included == project_mentions)) or "未提供 Xcode 工程文件"
                    evidence = [{"path": self.rel(path), "line": line, "excerpt": value}, {"path": self.rel(candidate), "line": 1, "excerpt": "storyboard"}]
                    evidence.extend({"path": self.rel(project), "line": 1, "excerpt": reason} for project, included, reason in membership if included == project_mentions)
                    self.add("IOS-001", status, "info" if status == "PASS" else "medium", "LaunchScreen.storyboard 配置", expected="启动 storyboard 存在并被 target 资源引用或同步目录纳入", actual=f"配置 {value}，文件 {self.rel(candidate)}；{reasons}", evidence=evidence, manual_check="核对上述证据缺口，在对应 target 的文件同步例外或 Copy Bundle Resources 中确认资源归属。" if status != "PASS" else None)
                else:
                    self.add("IOS-001", "FAIL", "blocker", "LaunchScreen storyboard 文件缺失", expected=f"存在 {name}.storyboard", actual=f"配置 {value} 但没有匹配文件", evidence=[{"path": self.rel(path), "line": line, "excerpt": value}])

        names: list[tuple[Path, str, int]] = []
        for path, values in self.plist_values.items():
            for key in ("CFBundleDisplayName", "CFBundleName"):
                value = as_string(values.get(key))
                if value:
                    names.append((path, value, self.plist_sources.get(path, {}).get(key, 1)))
        for path, data in self.json_values:
            for pointer, mapping in iter_dicts(data):
                pointer_lower = pointer.lower()
                is_app_name_pointer = "app_info_localizations" in pointer_lower or re.search(r"\.app(?:\.|$)", pointer_lower) is not None
                if "name" in mapping and isinstance(mapping["name"], str) and is_app_name_pointer:
                    names.append((path, mapping["name"], 1))
        project_products: list[str] = []
        for path in self.project_files:
            project_products.extend(
                value
                for value in (match.group(1).strip().strip('"') for match in re.finditer(r"PRODUCT_NAME\s*=\s*([^;]+);", self.source_texts[path]))
                if value and not value.startswith("$(")
            )
            if not project_products:
                project_products.append(path.parent.parent.stem if path.parent.name.endswith(".xcodeproj") else path.stem)
        resolved: list[tuple[Path, str, int]] = []
        for path, value, line in names:
            if value.startswith("$("):
                if project_products:
                    resolved.extend((path, product, line) for product in project_products)
                else:
                    self.add("IOS-002", "NOT_VERIFIABLE", "medium", "App 名称构建变量无法解析", expected="可解析用户可见名称", actual=value, evidence=[{"path": self.rel(path), "line": line, "excerpt": value}])
            else:
                resolved.append((path, value, line))
        if not resolved:
            self.add("IOS-002", "NOT_VERIFIABLE", "medium", "未找到用户可见 App 名称", expected="CFBundleDisplayName 或 App Store 名称", actual="没有可解析名称")
        else:
            seen: set[tuple[str, str]] = set()
            for path, value, line in resolved:
                key = (str(path), value)
                if key in seen:
                    continue
                seen.add(key)
                letters = len(re.findall(r"[A-Za-z]", value))
                minimum = int(self.policy.get("app_name_min_letters", 4))
                maximum = int(self.policy.get("app_name_max_letters", 7))
                ok = minimum <= letters <= maximum
                self.add("IOS-002", "PASS" if ok else "FAIL", "medium" if not ok else "info", "App 名称字母数量", expected=f"{minimum}–{maximum} 个英文字母", actual=f"{value}：{letters} 个英文字母", evidence=[{"path": self.rel(path), "line": line, "excerpt": value}])

    def _normalised_findings(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {rule_id: [] for rule_id in RULE_ORDER}
        auxiliary: list[dict[str, Any]] = []
        for finding in self.findings:
            rule_id = str(finding.get("id", ""))
            if rule_id in grouped:
                grouped[rule_id].append(dict(finding))
            else:
                auxiliary.append(dict(finding))

        ordered: list[dict[str, Any]] = []
        for rule_id in RULE_ORDER:
            matches = grouped[rule_id]
            if not matches:
                ordered.append({
                    "id": rule_id,
                    "status": "NOT_VERIFIABLE",
                    "severity": "medium",
                    "title": RULE_TITLES[rule_id],
                    "expected": "扫描器为该规则生成明确结论",
                    "actual": "未生成规则结论",
                    "evidence": [],
                    "manual_check": "检查扫描器输入与规则实现后重新运行。",
                })
                continue
            if len(matches) == 1:
                ordered.append(matches[0])
                continue

            details: list[dict[str, Any]] = []
            for match in matches:
                if match.get("details"):
                    details.extend(dict(detail) for detail in match["details"])
                else:
                    details.append({
                        "id": rule_id,
                        "status": match["status"],
                        "severity": match["severity"],
                        "label": match["title"],
                        "expected": match.get("expected", ""),
                        "actual": match.get("actual", ""),
                        "evidence": match.get("evidence", []),
                        "manual_check": match.get("manual_check"),
                    })
            ordered.append({
                "id": rule_id,
                "status": aggregate_status(matches),
                "severity": aggregate_severity(matches),
                "title": RULE_TITLES[rule_id],
                "expected": unique_text(match.get("expected") for match in matches),
                "actual": "；".join(f"{match['title']}：{match.get('actual') or '—'}" for match in matches),
                "evidence": unique_evidence(item for match in matches for item in match.get("evidence", [])),
                "manual_check": unique_text(match.get("manual_check") for match in matches) or None,
                "details": details,
            })
        return ordered + auxiliary

    def report(self, only_failures: bool = False) -> dict[str, Any]:
        findings = self._normalised_findings()
        if only_failures:
            visible_findings: list[dict[str, Any]] = []
            for finding in findings:
                if finding["status"] != "FAIL":
                    continue
                visible = dict(finding)
                if finding.get("details") is not None:
                    failed_details = [dict(detail) for detail in finding["details"] if detail.get("status") == "FAIL"]
                    visible["details"] = failed_details
                    if failed_details:
                        visible["actual"] = "；".join(f"{detail['label']}：{detail['actual']}" for detail in failed_details)
                        visible["manual_check"] = unique_text(detail.get("manual_check") for detail in failed_details) or None
                visible_findings.append(visible)
            findings = visible_findings
        counts = {status: sum(1 for finding in findings if finding["status"] == status) for status in STATUS_ORDER}
        return {
            "schema_version": "2.0",
            "project_root": ".",
            "only_failures": only_failures,
            "summary": counts,
            "findings": findings,
        }


def load_policy(path: Path | None) -> dict[str, Any]:
    if path is None:
        return dict(DEFAULT_POLICY)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取策略文件 {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("策略文件根节点必须是 JSON object")
    policy = dict(DEFAULT_POLICY)
    policy.update(data)
    return policy


def markdown_cell(value: Any) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", "<br>")


def markdown_report(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    only_failures = bool(report.get("only_failures"))
    status_labels = {
        "PASS": "通过 (PASS)",
        "FAIL": "未通过 (FAIL)",
        "NOT_VERIFIABLE": "静态无法确认 (NOT_VERIFIABLE)",
        "WARN": "警告 (WARN)",
    }
    lines = [
        "# iOS A 面审核检查报告",
        "",
        f"项目根目录：`{report['project_root']}`",
        f"规则版本：`{report['schema_version']}`",
        "",
        "## 未通过项汇总" if only_failures else "## 汇总",
        "",
        f"- 通过 (PASS)：{summary.get('PASS', 0)}",
        f"- 未通过 (FAIL)：{summary.get('FAIL', 0)}",
        f"- 静态无法确认 (NOT_VERIFIABLE)：{summary.get('NOT_VERIFIABLE', 0)}",
        f"- 警告 (WARN)：{summary.get('WARN', 0)}",
        "",
        "## 完整检查清单" if report["findings"] else "未生成检查项。",
        "",
    ]
    if report["findings"]:
        lines.extend([
            "| 状态 | ID | 检查项 | 结论 |",
            "|---|---|---|---|",
        ])
        for finding in report["findings"]:
            lines.append(
                f"| {status_labels.get(finding['status'], finding['status'])} | `{markdown_cell(finding['id'])}` | "
                f"{markdown_cell(finding['title'])} | {markdown_cell(finding['actual'])} |"
            )
        lines.extend(["", "## 详细结果", ""])

    for finding in report["findings"]:
        lines.extend([
            f"### {status_labels.get(finding['status'], finding['status'])} · `{finding['id']}` · {finding['title']}",
            "",
            f"- 严重级别：{finding['severity']}",
            f"- 期望：{finding['expected'] or '—'}",
            f"- 实际：{finding['actual'] or '—'}",
        ])
        if finding.get("details"):
            lines.append("- 子检查：")
            for detail in finding["details"]:
                lines.append(
                    f"  - {status_labels.get(detail['status'], detail['status'])} · "
                    f"`{detail.get('id', finding['id'])}` · {detail['label']}：{detail['actual']}"
                )
        if finding.get("evidence"):
            lines.append("- 证据：")
            for evidence in finding["evidence"]:
                lines.append(f"  - `{evidence.get('path')}:{evidence.get('line')}`：{evidence.get('excerpt', '')}")
        if finding.get("manual_check"):
            lines.append(f"- 人工验证：{finding['manual_check']}")
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读检查 iOS A 面上架风险")
    parser.add_argument("project_root", type=Path)
    parser.add_argument("--config", type=Path, default=None, help="项目级策略 JSON")
    parser.add_argument("--output-dir", type=Path, default=None, help="报告目录；默认使用系统临时目录")
    parser.add_argument("--format", choices=("markdown", "json", "pdf", "both", "all"), default="pdf")
    parser.add_argument("--allow-project-output", action="store_true", help="明确允许把报告写入项目目录")
    parser.add_argument("--fail-on-not-verifiable", action="store_true", help="将 NOT_VERIFIABLE 也作为非零退出")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.project_root.expanduser().resolve()
    if not root.is_dir():
        print(f"项目目录不存在：{root}", file=sys.stderr)
        return 2
    skill_root = Path(__file__).resolve().parents[1]
    invocation_cwd = Path.cwd()
    try:
        updated = ensure_latest(skill_root)
    except UpdateError as error:
        print(f"技能版本检查失败，未执行审计：{error}", file=sys.stderr)
        return 2
    if updated:
        # Run a fresh interpreter so neither scanner nor renderer code comes from the old release.
        print("技能已更新，正在使用新版本重新执行审计。", flush=True)
        return subprocess.call(
            [sys.executable, str(skill_root / "scripts" / "audit_ios_a_side.py"),
             *(list(argv) if argv is not None else sys.argv[1:])],
            cwd=str(invocation_cwd),
        )
    try:
        policy = load_policy(args.config.expanduser().resolve() if args.config else None)
        auditor = Auditor(root, policy).run()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    render_pdf = None
    if args.format in {"pdf", "all"}:
        try:
            try:
                from .render_audit_pdf import preflight_pdf, render_audit_pdf
            except ImportError:
                from render_audit_pdf import preflight_pdf, render_audit_pdf
            preflight_pdf()
            render_pdf = render_audit_pdf
        except (ImportError, RuntimeError) as error:
            print(f"无法生成 PDF：{error}", file=sys.stderr)
            return 2

    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else Path(tempfile.mkdtemp(prefix="ios-aside-review-"))
    if is_within(output_dir, root) and not args.allow_project_output:
        print(f"为保护目标项目，默认禁止在项目内写入报告：{output_dir}", file=sys.stderr)
        return 2
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        report = auditor.report()
        written: list[Path] = []
        if args.format in {"json", "both", "all"}:
            json_path = output_dir / "ios-aside-review.json"
            json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written.append(json_path)
        if args.format in {"markdown", "both", "all"}:
            markdown_path = output_dir / "ios-aside-review.md"
            markdown_path.write_text(markdown_report(report), encoding="utf-8")
            written.append(markdown_path)
        if args.format in {"pdf", "all"} and render_pdf is not None:
            pdf_path = output_dir / "ios-aside-review.pdf"
            render_pdf(report, pdf_path)
            written.append(pdf_path)
    except (OSError, RuntimeError) as error:
        print(f"无法写入报告：{error}", file=sys.stderr)
        return 2

    print("项目根目录：.")
    print("汇总：" + ", ".join(f"{key}={value}" for key, value in report["summary"].items()))
    for path in written:
        print(f"报告：{path}")
    if report["summary"].get("FAIL", 0) or (args.fail_on_not_verifiable and report["summary"].get("NOT_VERIFIABLE", 0)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
