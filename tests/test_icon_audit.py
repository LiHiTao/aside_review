import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts.audit_ios_a_side import Auditor
from scripts.icon_api import ICON_API_URL, IconApiError, check_icon, icon_pixels


class IconAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.root = directory / "App"
        self.root.mkdir()
        token = patch("scripts.icon_audit.read_token", return_value="a" * 40)
        token.start()
        self.addCleanup(token.stop)

    def icon(self, name, color):
        folder = self.root / "Assets.xcassets" / f"{name}.appiconset"
        folder.mkdir(parents=True)
        (folder / "Contents.json").write_text(json.dumps({"images": [
            {"filename": "legacy.png", "idiom": "iphone", "size": "60x60", "scale": "2x"},
            {"filename": "icon.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"},
            {"filename": "dark.png", "idiom": "universal", "size": "1024x1024",
             "appearances": [{"appearance": "luminosity", "value": "dark"}]},
        ]}))
        Image.new("RGB", (120, 120), "gray").save(folder / "legacy.png")
        path = folder / "icon.png"
        Image.new("RGB", (1024, 1024), color).save(path)
        return path

    def finding(self):
        report = Auditor(self.root).run().report()
        self.assertEqual(report["schema_version"], "2.0")
        self.assertEqual(len(report["findings"]), 21)
        return next(item for item in report["findings"] if item["id"] == "IOS-003")

    def test_explicit_primary_icon_and_top_five(self):
        self.icon("AppIcon", "white")
        primary = self.icon("ReleaseIcon", "black")
        (self.root / "Release.xcconfig").write_text("ASSETCATALOG_COMPILER_APPICON_NAME = ReleaseIcon\n")
        matches = [{"name": f"reference {i}", "score": 100 - i} for i in range(5)]
        with patch("scripts.icon_audit.check_icon", return_value=(matches, False)) as request:
            result = self.finding()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["evidence"][0]["path"], primary.relative_to(self.root).as_posix())
        self.assertEqual(result["details"][0]["matches"], matches)
        request.assert_called_once_with(bytes(128 * 128 * 3), "App", "a" * 40)

    def test_threshold_and_enrollment_status(self):
        self.icon("AppIcon", "black")
        for score, added, status in ((84.9, True, "PASS"), (85, False, "PASS"), (85.1, False, "FAIL")):
            with self.subTest(score=score), patch(
                "scripts.icon_audit.check_icon", return_value=([{"name": "Ref", "score": score}], added)
            ):
                result = self.finding()
                self.assertEqual(result["status"], status)
                self.assertIs(result["details"][0]["added"], added)
                self.assertIn(f"入库 {int(added)} 个", result["actual"])

    def test_duplicate_configurations_upload_once(self):
        self.icon("AppIcon", "black")
        self.icon("ReleaseIcon", "black")
        (self.root / "App.xcconfig").write_text(
            "ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon\n"
            "ASSETCATALOG_COMPILER_APPICON_NAME[sdk=iphoneos*] = ReleaseIcon\n"
        )
        with patch("scripts.icon_audit.check_icon", return_value=([{"name": "Ref", "score": 84}], True)) as request:
            result = self.finding()
        self.assertEqual(result["status"], "PASS")
        request.assert_called_once()

    def test_unavailable_prerequisites_do_not_upload(self):
        with patch("scripts.icon_audit.check_icon") as request:
            self.assertEqual(self.finding()["status"], "NOT_VERIFIABLE")
            icon = self.icon("AppIcon", "black")
            with patch("scripts.icon_audit.read_token", side_effect=IconApiError("missing token")):
                self.assertEqual(self.finding()["status"], "NOT_VERIFIABLE")
            icon.write_bytes(b"broken image")
            self.assertEqual(self.finding()["status"], "NOT_VERIFIABLE")
            request.assert_not_called()

    def test_service_error_needs_review(self):
        self.icon("AppIcon", "black")
        with patch("scripts.icon_audit.check_icon", side_effect=IconApiError("service unavailable")):
            result = self.finding()
        self.assertEqual(result["status"], "NOT_VERIFIABLE")
        self.assertIn("service unavailable", result["actual"])

    def test_known_high_score_wins_despite_missing_configured_asset(self):
        self.icon("AppIcon", "black")
        (self.root / "Release.xcconfig").write_text("ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon\n")
        (self.root / "Debug.xcconfig").write_text("ASSETCATALOG_COMPILER_APPICON_NAME = Missing\n")
        for score, expected in ((100, "FAIL"), (85, "NOT_VERIFIABLE")):
            with self.subTest(score=score), patch(
                "scripts.icon_audit.check_icon", return_value=([{"name": "Ref", "score": score}], False)
            ):
                result = self.finding()
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["details"][-1]["status"], "NOT_VERIFIABLE")

    def test_api_posts_pixels_and_keeps_only_match_fields(self):
        icon = self.icon("AppIcon", "white")
        pixels = icon_pixels(icon)
        with patch("scripts.icon_api.build_opener") as build:
            opener = build.return_value
            response = opener.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({
                "matches": [{"name": "Reference", "score": 80, "image": "preview"}],
                "added": True,
            }).encode()
            self.assertEqual(check_icon(pixels, "Example App", "b" * 40),
                             ([{"name": "Reference", "score": 80}], True))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ICON_API_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "b" * 40)
        self.assertEqual(request.get_header("User-agent"), "ios-aside-review icon audit")
        self.assertEqual(request.get_header("X-app-name"), "Example%20App")
        self.assertEqual(request.data, bytes([255]) * (128 * 128 * 3))

    def test_api_rejects_malformed_response(self):
        with patch("scripts.icon_api.build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            for payload in ({"matches": [], "added": False},
                            {"matches": [{"name": "Ref", "score": float("nan")}], "added": False},
                            {"matches": [{"name": "Ref", "score": 84.9}], "added": False},
                            {"matches": [{"name": "Ref", "score": 85}], "added": True},
                            {"matches": [{"name": "Ref", "score": 80}]}):
                response.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(IconApiError):
                    check_icon(bytes(128 * 128 * 3), "App", "b" * 40)
