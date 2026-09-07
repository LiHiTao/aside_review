#!/usr/bin/env python3
import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts.audit_ios_a_side import (  # noqa: E402
    Auditor,
    DEFAULT_POLICY,
    RULE_ORDER,
    STATUS_ORDER,
    markdown_report,
    parse_args,
)


PRICES = ["0.99", "2.99", "9.99", "19.99", "49.99", "99.99"]


def write_fixture(root: Path, broken: bool = False) -> None:
    (root / "StoreConfig").mkdir(parents=True)
    (root / "Carvory" / "Base.lproj").mkdir(parents=True)
    products = []
    code_lines = ["import StoreKit", "import AppTrackingTransparency", "import AVFoundation", "import Photos", "import UserNotifications", "", "final class IAP {", "    let packs = ["]
    amounts = [100, 300, 1000, 2200, 6000, 15000]
    for index, (price, amount) in enumerate(zip(PRICES, amounts), start=1):
        product_id = f"com.example.coins{amount}"
        tier = f"{index:02d}"
        products.append(
            {
                "product_id": product_id,
                "price_usd": price,
                "coins": amount,
                "reference_name": f"Coins {amount} Tier {tier}",
                "localizations": [{"locale": "en-US", "name": f"Coins {amount} Tier {tier}", "description": f"{amount} coins for in-app features."}],
                "submit_for_review": not broken,
            }
        )
        code_lines.append(f'        (id: "{product_id}", price: "${price}", coins: {amount}, title: "Coins {amount} Tier {tier}"),')
    code_lines.extend([
        "    ]",
        "    func requestTracking() { ATTrackingManager.requestTrackingAuthorization { _ in } }",
        "    func requestPermissions() {",
        "        _ = AVCaptureDevice.authorizationStatus(for: .video)",
        "        _ = PHPhotoLibrary.authorizationStatus(for: .readWrite)",
        "        _ = AVAudioSession.sharedInstance().recordPermission",
        "        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }",
        '        let pushPurpose = "Receive activity updates through notifications."',
        "    }",
        "}",
    ])
    if broken:
        code_lines.insert(2, "    func restorePurchases() {}")
        code_lines.insert(3, "    final class NotificationProxy: NSObject, UNUserNotificationCenterDelegate {}")
    (root / "Carvory" / "IAP.swift").write_text("\n".join(code_lines), encoding="utf-8")

    (root / "StoreConfig" / "app_store_config.json").write_text(json.dumps({"app": {"download_price": "free", "name": "Aside"}, "iap_products": products}, indent=2), encoding="utf-8")
    plist = {
        "CFBundleDisplayName": "Aside",
        "CFBundleName": "Aside",
        "NSCameraUsageDescription": "Aside uses the camera to take a private profile photo.",
        "NSPhotoLibraryUsageDescription": "Aside lets you choose photos for your private journal.",
        "NSMicrophoneUsageDescription": "Aside uses the microphone to dictate an editable journal highlight.",
        "NSUserTrackingUsageDescription": "Aside uses tracking data to improve advertising relevance.",
        "UILaunchStoryboardName": "LaunchScreen",
    }
    (root / "Carvory" / "Info.plist").write_bytes(plistlib.dumps(plist))
    (root / "Carvory" / "Base.lproj" / "LaunchScreen.storyboard").write_text("<document launchScreen=\"YES\"></document>\n", encoding="utf-8")
    (root / "Carvory" / "Carvory.entitlements").write_bytes(plistlib.dumps({"aps-environment": "development"}))
    (root / "Carvory.xcodeproj").mkdir()
    (root / "Carvory.xcodeproj" / "project.pbxproj").write_text(
        "LaunchScreen.storyboard\nPRODUCT_NAME = Aside;\ncom.apple.Push = { enabled = 1; };\n",
        encoding="utf-8",
    )
    (root / "StoreConfig" / "metadata.txt").write_text("Optional coins are available through Apple In-App Purchase. Coins unlock journal capacity and activities.", encoding="utf-8")


class AuditTests(unittest.TestCase):
    def test_price_order_uses_original_catalogue_order_without_name_ordinals(self) -> None:
        cases = [
            (PRICES, {}, "PASS"),
            ([PRICES[1], PRICES[0], *PRICES[2:]], {}, "FAIL"),
            ([*PRICES[:2], None, *PRICES[3:]], {}, "NOT_VERIFIABLE"),
            (["1.99", "4.99"], {"required_prices_usd": ["1.99", "4.99"]}, "PASS"),
            (PRICES[1:], {}, "FAIL"),
        ]
        for prices, policy, expected in cases:
            with self.subTest(prices=prices), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                products = [{"product_id": f"com.example.{chr(97 + i)}", "name": "Small bundle", "price_usd": price} for i, price in enumerate(prices)]
                (root / "store.json").write_text(json.dumps({"iap_products": products}))
                auditor = Auditor(root, policy).run()
                finding = next(f for f in auditor.findings if f["id"] == "IAP-SUMMARY")
                check = next(f for f in finding["details"] if f["id"] == "IAP-007")
                self.assertEqual(check["status"], expected, check)

    def test_price_order_checks_each_catalogue_independently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            products = [{"product_id": f"com.example.{i}", "price_usd": price} for i, price in enumerate(PRICES)]
            (root / "store.json").write_text(json.dumps({"current": {"iap_products": products}, "backup": {"iap_products": products}}))
            auditor = Auditor(root).run()
            summary = next(f for f in auditor.findings if f["id"] == "IAP-SUMMARY")
            self.assertEqual(next(f for f in summary["details"] if f["id"] == "IAP-007")["status"], "PASS")

    def test_app_description_does_not_require_purchase_copy_or_iap(self) -> None:
        for file_name, body in (
            ("metadata.txt", "A peaceful place to collect your favorite films."),
            ("metadata/descriptions/en-US.txt", "Keep a personal film journal."),
            ("app_store_config.json", json.dumps({"app": {"localizations": [{"description": "Your film journal."}]}})),
            ("config.json", json.dumps({"version": {"localizations": {"en-US": {"description": "Your film journal."}}}})),
        ):
            with self.subTest(file_name=file_name), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                path = root / file_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                check = next(f for f in Auditor(root).run().findings if f["id"] == "META-001")
                self.assertEqual(check["status"], "PASS", check)

    def test_empty_missing_and_unrelated_descriptions_are_not_passed(self) -> None:
        cases = [
            ({"metadata.txt": "  \n"}, "FAIL"),
            ({"app_store_config.json": json.dumps({"app": {"description": ""}})}, "FAIL"),
            ({"config.json": json.dumps({"description_file": "missing.txt"})}, "NOT_VERIFIABLE"),
            ({}, "NOT_VERIFIABLE"),
            ({"README.md": "An excellent app.", "privacy.txt": "Your privacy matters.", "review_notes.txt": "Buy credits."}, "NOT_VERIFIABLE"),
            ({"app_store_config.json": json.dumps({"iap_products": [{"product_id": "com.example.pack", "price_usd": "0.99", "description": "Buy useful credits.", "localizations": [{"description": "A product description."}]}], "review_information": {"description": "Review this app."}})}, "NOT_VERIFIABLE"),
        ]
        for files, expected in cases:
            with self.subTest(files=files), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                for filename, body in files.items():
                    (root / filename).write_text(body)
                check = next(f for f in Auditor(root).run().findings if f["id"] == "META-001")
                self.assertEqual(check["status"], expected, check)

    def test_description_file_rejects_legal_and_review_parent_directories(self) -> None:
        for folder in ("privacy", "terms", "review_notes"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                (root / folder).mkdir()
                (root / folder / "en-US.txt").write_text("This is not an application description.")
                (root / "config.json").write_text(json.dumps({"description_file": f"{folder}/en-US.txt"}))
                check = next(f for f in Auditor(root).run().findings if f["id"] == "META-001")
                self.assertEqual(check["status"], "NOT_VERIFIABLE", check)

    def test_referenced_conventional_description_file_is_counted_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            (root / "metadata").mkdir()
            (root / "metadata" / "en-US.txt").write_text("Your personal film journal.")
            (root / "config.json").write_text(json.dumps({"description_file": "metadata/en-US.txt"}))
            check = next(f for f in Auditor(root).run().findings if f["id"] == "META-001")
            self.assertEqual(check["status"], "PASS", check)
            self.assertEqual(check["actual"], "检测到 1 份非空应用描述")
            self.assertEqual(len(check["evidence"]), 1)

    def test_description_file_resolves_relative_to_its_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            (root / "StoreConfig").mkdir()
            (root / "copy.txt").write_text("Capture your favorite film memories.")
            (root / "StoreConfig" / "config.json").write_text(json.dumps({"version": {"description_file": "../copy.txt"}}))
            check = next(f for f in Auditor(root).run().findings if f["id"] == "META-001")
            self.assertEqual(check["status"], "PASS", check)
            self.assertEqual(check["evidence"][0]["path"], "copy.txt")

    def test_clean_fixture_has_no_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            self.assertEqual(report["summary"]["FAIL"], 0, report["findings"])
            self.assertGreater(report["summary"]["PASS"], 0)

    def test_notification_ids_do_not_expand_nine_product_catalogue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            config_path = root / "StoreConfig" / "app_store_config.json"
            config = json.loads(config_path.read_text())
            products = config["iap_products"]
            products.extend(dict(products[0]) for _ in range(3))
            for index, product in enumerate(products, 1):
                product["product_id"] = f"com.endrqx.endrq{index}"
            config_path.write_text(json.dumps(config))
            # Known configuration IDs need no price or naming heuristic.
            (root / "Carvory" / "IAP.swift").write_text("\n".join(
                f'let item{index} = Pack(id: "{product["product_id"]}")'
                for index, product in enumerate(products)
            ))
            (root / "Carvory" / "DrillBell.swift").write_text(
                'import StoreKit\n'
                r'let alert = Notice(id: "erx.study.\(study.studyId)")' + "\n"
                'let staticAlert = Notice(id: "erx.study.reminder")\n'
                'let keywordAlert = Notice(id: "coin.pack.sku")\n'
                'let physicalItem = (id: "catalog.Book", price: "$9.99")\n'
            )
            auditor = Auditor(root, DEFAULT_POLICY).run()
            self.assertEqual(len(auditor.code_products), 9)
            self.assertEqual(auditor.dynamic_code_products, [])
            checks = next(f for f in auditor.findings if f["id"] == "IAP-SUMMARY")["details"]
            by_id = {f["id"]: f for f in checks}
            self.assertEqual(by_id["IAP-002"]["status"], "PASS")
            self.assertIn("代码 9 项 / JSON 9 项", by_id["IAP-002"]["actual"])
            self.assertEqual(by_id["IAP-006"]["status"], "PASS")

    def test_unconfigured_product_ids_remain_detectable(self) -> None:
        for declaration in (
            'let productID = "com.example.Extra"',
            'let item = StoreProduct(id: "com.example.Extra")',
            'let item = (id: "com.example.Extra", price: "$0.99", coins: 100)',
        ):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                write_fixture(root)
                (root / "Carvory" / "Extra.swift").write_text(declaration)
                auditor = Auditor(root, DEFAULT_POLICY).run()
                checks = next(f for f in auditor.findings if f["id"] == "IAP-SUMMARY")["details"]
                by_id = {f["id"]: f for f in checks}
                self.assertEqual(by_id["IAP-002"]["status"], "FAIL")
                self.assertEqual(by_id["IAP-006"]["status"], "FAIL")

    def test_dynamic_product_ids_do_not_become_false_literal_failures_or_passes(self) -> None:
        for declaration in (
            r'let productID = "com.example.\(tier.productId)"',
            r'let item = StoreProduct(id: "com.example.\(tier.productId)")',
            'let productID = "com.example." + suffix',
        ):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                write_fixture(root)
                (root / "Carvory" / "Dynamic.swift").write_text(declaration)
                auditor = Auditor(root, DEFAULT_POLICY).run()
                self.assertEqual(len(auditor.code_products), 6)
                self.assertEqual(len(auditor.dynamic_code_products), 1)
                checks = next(f for f in auditor.findings if f["id"] == "IAP-SUMMARY")["details"]
                by_id = {f["id"]: f for f in checks}
                self.assertEqual(by_id["IAP-002"]["status"], "NOT_VERIFIABLE")
                self.assertEqual(by_id["IAP-006"]["status"], "NOT_VERIFIABLE")

    def test_broken_fixture_detects_submission_restore_and_delegate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root, broken=True)
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            failures = {finding["id"] for finding in report["findings"] if finding["status"] == "FAIL"}
            self.assertIn("IAP-SUMMARY", failures)
            self.assertIn("IAP-009", failures)
            self.assertIn("AB-001", failures)

            iap_summary = next(finding for finding in report["findings"] if finding["id"] == "IAP-SUMMARY")
            self.assertFalse(any(detail["id"] == "IAP-004" for detail in iap_summary.get("details", [])))
            self.assertNotIn("金币", iap_summary["actual"])
            self.assertNotIn("积分", iap_summary["actual"])

    def test_iap_checks_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            iap_findings = [finding for finding in report["findings"] if finding["id"].startswith("IAP-") and finding["id"] != "IAP-009"]
            self.assertEqual([finding["id"] for finding in iap_findings], ["IAP-SUMMARY"])
            titles = [finding["title"] for finding in report["findings"]]
            self.assertFalse(any("商品 com.example" in title for title in titles))

    def test_missing_launch_screen_is_reported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            (root / "Carvory" / "Base.lproj" / "LaunchScreen.storyboard").unlink()
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            failures = {finding["id"] for finding in report["findings"] if finding["status"] == "FAIL"}
            self.assertIn("IOS-001", failures)

    def test_permissions_accept_xcode_build_setting_without_api_request_checks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            plist_path = root / "Carvory" / "Info.plist"
            plist = plistlib.loads(plist_path.read_bytes())
            del plist["NSCameraUsageDescription"]
            plist_path.write_bytes(plistlib.dumps(plist))
            project_path = root / "Carvory.xcodeproj" / "project.pbxproj"
            project_path.write_text(
                project_path.read_text(encoding="utf-8")
                + 'INFOPLIST_KEY_NSCameraUsageDescription = "Aside uses the camera to take a private profile photo.";\n',
                encoding="utf-8",
            )
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            failures = [finding for finding in report["findings"] if finding["status"] == "FAIL"]
            self.assertFalse(any(finding["id"] in {"PERM-001", "ATT-001"} for finding in failures))
            permission_config = next(finding for finding in report["findings"] if finding["id"] == "PERM-001")
            camera_config = next(detail for detail in permission_config["details"] if detail["label"] == "相机")
            self.assertEqual(camera_config["evidence"][0]["path"], "Carvory.xcodeproj/project.pbxproj")

    def test_permissions_do_not_require_runtime_att_request_or_declaration_without_api(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            source_path = root / "Carvory" / "IAP.swift"
            source = source_path.read_text(encoding="utf-8").replace(
                "func requestTracking() { ATTrackingManager.requestTrackingAuthorization { _ in } }",
                "func requestTracking() {}",
            )
            source_path.write_text(source, encoding="utf-8")
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            self.assertFalse(any(finding["id"] == "ATT-001" and finding["status"] == "FAIL" for finding in report["findings"]))

    def test_push_only_checks_configuration(self) -> None:
        for config_kind in ("entitlements", "capability", "missing"):
            with self.subTest(config_kind=config_kind), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                write_fixture(root)
                source_path = root / "Carvory" / "IAP.swift"
                source_path.write_text(source_path.read_text().replace(
                    '        let pushPurpose = "Receive activity updates through notifications."', ""
                ))
                if config_kind != "entitlements":
                    (root / "Carvory" / "Carvory.entitlements").unlink()
                if config_kind != "capability":
                    project_path = root / "Carvory.xcodeproj" / "project.pbxproj"
                    project_path.write_text(project_path.read_text().replace("com.apple.Push = { enabled = 1; };", ""))
                findings = {item["id"]: item for item in Auditor(root, DEFAULT_POLICY).run().report()["findings"]}
                push = next(item for item in findings["PERM-001"]["details"] if item["label"] == "Push")
                self.assertEqual(push["status"], "FAIL" if config_kind == "missing" else "PASS")
                self.assertEqual(findings["PERM-002"]["status"], "PASS")
                self.assertFalse(any(item["label"] == "Push" for item in findings["PERM-002"]["details"]))

    def test_push_rejects_invalid_or_commented_configuration(self) -> None:
        cases = (
            ("", "", "FAIL"),
            ("invalid", "", "FAIL"),
            (None, "// aps-environment = development;\n// com.apple.Push = { enabled = 1; };", "FAIL"),
            (None, 'aps-environment = "";', "FAIL"),
            (None, "aps-environment = production-invalid;", "FAIL"),
            (None, "com.apple.Push = { enabled = 0; };", "FAIL"),
            (None, "/* com.apple.Push = { enabled = 1; }; */", "FAIL"),
            ("production", "", "PASS"),
            (None, '"aps-environment" = "production";', "PASS"),
            (None, "aps-environment = development;", "PASS"),
        )
        for environment, config, expected in cases:
            with self.subTest(environment=environment, config=config), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                write_fixture(root)
                entitlement = root / "Carvory" / "Carvory.entitlements"
                entitlement.write_bytes(plistlib.dumps({} if environment is None else {"aps-environment": environment}))
                project = root / "Carvory.xcodeproj" / "project.pbxproj"
                project.write_text(config)
                findings = {item["id"]: item for item in Auditor(root, DEFAULT_POLICY).run().report()["findings"]}
                push = next(item for item in findings["PERM-001"]["details"] if item["label"] == "Push")
                self.assertEqual(push["status"], expected)

    def test_att_configuration_and_copy_without_runtime_request(self) -> None:
        for purpose in ("Aside uses tracking data to improve advertising relevance.", "Permission", "Improve discovery across the app.", None):
            with self.subTest(purpose=purpose), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                write_fixture(root)
                source_path = root / "Carvory" / "IAP.swift"
                source_path.write_text(source_path.read_text().replace(
                    "func requestTracking() { ATTrackingManager.requestTrackingAuthorization { _ in } }", ""
                ))
                plist_path = root / "Carvory" / "Info.plist"
                values = plistlib.loads(plist_path.read_bytes())
                if purpose is None:
                    values.pop("NSUserTrackingUsageDescription")
                else:
                    values["NSUserTrackingUsageDescription"] = purpose
                plist_path.write_bytes(plistlib.dumps(values))
                findings = {item["id"]: item for item in Auditor(root, DEFAULT_POLICY).run().report()["findings"]}
                self.assertEqual(findings["ATT-001"]["status"], "FAIL" if purpose is None else "PASS")
                self.assertEqual(findings["ATT-002"]["status"], "NOT_VERIFIABLE" if purpose is None else "PASS")

    def test_att_copy_only_rejects_empty_or_personalized_recommendation_templates(self) -> None:
        cases = (
            ("", "FAIL"),
            ("   \n\t", "FAIL"),
            ("允许追踪以提供个性化推荐。", "FAIL"),
            ("为您提供个性化的内容。", "FAIL"),
            ("Allow tracking for personalized recommendations.", "FAIL"),
            ("We provide personalised content for you.", "FAIL"),
            ("Allow us to personalize your recommendations.", "FAIL"),
            ("Personalized recommendations", "FAIL"),
            ("Permission", "PASS"),
            ("Improve discovery across the app.", "PASS"),
            ("We use tracking data to measure personalized ads across other companies' apps.", "PASS"),
            ("Vomifib asks for your optional permission before any future product measurement, so your private care-journaling experience stays privacy-controlled.", "PASS"),
        )
        for purpose, expected in cases:
            with self.subTest(purpose=purpose), tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
                root = Path(directory)
                write_fixture(root)
                plist_path = root / "Carvory" / "Info.plist"
                values = plistlib.loads(plist_path.read_bytes())
                values["NSUserTrackingUsageDescription"] = purpose
                plist_path.write_bytes(plistlib.dumps(values))
                findings = {item["id"]: item for item in Auditor(root, DEFAULT_POLICY).run().report()["findings"]}
                self.assertEqual(findings["ATT-001"]["status"], "PASS")
                self.assertEqual(findings["ATT-002"]["status"], expected)
                self.assertEqual(findings["PERM-002"]["status"], "PASS")

    def test_permission_purpose_does_not_require_theme_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            plist_path = root / "Carvory" / "Info.plist"
            plist = plistlib.loads(plist_path.read_bytes())
            plist["NSCameraUsageDescription"] = "Aside uses the camera to capture images."
            plist_path.write_bytes(plistlib.dumps(plist))
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            self.assertFalse(any(finding["id"] == "PERM-002" and finding["status"] == "FAIL" for finding in report["findings"]))

    def test_report_paths_are_relative_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            plist_path = root / "Carvory" / "Info.plist"
            plist = plistlib.loads(plist_path.read_bytes())
            del plist["NSCameraUsageDescription"]
            plist_path.write_bytes(plistlib.dumps(plist))
            report = Auditor(root, DEFAULT_POLICY).run().report()
            self.assertEqual(report["project_root"], ".")
            self.assertTrue(report["findings"])
            self.assertTrue(all(not Path(item["path"]).is_absolute() for finding in report["findings"] for item in finding["evidence"]))

    def test_default_report_contains_complete_checklist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root, broken=True)
            report = Auditor(root, DEFAULT_POLICY).run().report()
            self.assertEqual(report["schema_version"], "2.0")
            self.assertFalse(report["only_failures"])
            self.assertEqual(tuple(report["summary"]), STATUS_ORDER)
            self.assertEqual(tuple(finding["id"] for finding in report["findings"]), RULE_ORDER)
            rendered = markdown_report(report)
            self.assertIn("完整检查清单", rendered)
            self.assertIn("通过 (PASS)", rendered)
            self.assertIn("静态无法确认 (NOT_VERIFIABLE)", rendered)
            self.assertIn("警告 (WARN)", rendered)

    def test_legacy_failure_only_report_remains_available(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root, broken=True)
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=True)
            self.assertTrue(report["only_failures"])
            self.assertTrue(report["findings"])
            self.assertTrue(all(finding["status"] == "FAIL" for finding in report["findings"]))
            self.assertEqual(set(report["summary"]), set(STATUS_ORDER))

    def test_iap_and_permission_groups_contain_all_subchecks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            report = Auditor(root, DEFAULT_POLICY).run().report()
            iap = next(finding for finding in report["findings"] if finding["id"] == "IAP-SUMMARY")
            self.assertEqual(
                [detail["id"] for detail in iap["details"]],
                ["IAP-001", "IAP-002", "IAP-003", "IAP-005", "IAP-006", "IAP-007", "IAP-008"],
            )
            permission = next(finding for finding in report["findings"] if finding["id"] == "PERM-001")
            self.assertEqual([detail["label"] for detail in permission["details"]], ["相机", "相册", "麦克风", "Push"])

    def test_meta_submission_reuses_iap_submission_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root, broken=True)
            report = Auditor(root, DEFAULT_POLICY).run().report()
            iap = next(finding for finding in report["findings"] if finding["id"] == "IAP-SUMMARY")
            iap_submission = next(detail for detail in iap["details"] if detail["id"] == "IAP-003")
            metadata_submission = next(finding for finding in report["findings"] if finding["id"] == "META-003")
            self.assertEqual(metadata_submission["status"], iap_submission["status"])
            self.assertEqual(metadata_submission["actual"], iap_submission["actual"])
            self.assertEqual(metadata_submission["evidence"], iap_submission["evidence"])

    def test_missing_submission_flag_is_not_verifiable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            config_path = root / "StoreConfig" / "app_store_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            for product in config["iap_products"]:
                product.pop("submit_for_review")
            config_path.write_text(json.dumps(config), encoding="utf-8")

            report = Auditor(root, DEFAULT_POLICY).run().report()
            iap = next(finding for finding in report["findings"] if finding["id"] == "IAP-SUMMARY")
            submission = next(detail for detail in iap["details"] if detail["id"] == "IAP-003")
            metadata_submission = next(finding for finding in report["findings"] if finding["id"] == "META-003")
            self.assertEqual(submission["status"], "NOT_VERIFIABLE")
            self.assertIn("缺少字段", submission["actual"])
            self.assertEqual(metadata_submission["status"], "NOT_VERIFIABLE")

    def test_cli_formats_include_pdf_and_all(self) -> None:
        self.assertEqual(parse_args(["/tmp/project"]).format, "pdf")
        self.assertEqual(parse_args(["/tmp/project", "--format", "pdf"]).format, "pdf")
        self.assertEqual(parse_args(["/tmp/project", "--format", "all"]).format, "all")

    def test_age_rating_rule_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root, broken=True)
            plist_path = root / "Carvory" / "Info.plist"
            plist = plistlib.loads(plist_path.read_bytes())
            del plist["NSCameraUsageDescription"]
            plist_path.write_bytes(plistlib.dumps(plist))
            config_path = root / "StoreConfig" / "app_store_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["app"]["age_rating"] = "12+"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            (root / "StoreConfig" / "metadata.txt").write_text(
                "Optional coins are available through Apple In-App Purchase. Coins unlock activity capacity. Alcohol references require review.",
                encoding="utf-8",
            )

            report = Auditor(root, DEFAULT_POLICY).run().report()
            self.assertNotIn("META-004", {finding["id"] for finding in report["findings"]})
            self.assertEqual(report["summary"]["WARN"], 0)

    def test_empty_sensitive_policy_is_not_verifiable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            policy = dict(DEFAULT_POLICY)
            policy["sensitive_terms"] = {}
            report = Auditor(root, policy).run().report()
            finding = next(finding for finding in report["findings"] if finding["id"] == "SENSITIVE-001")
            self.assertEqual(finding["status"], "NOT_VERIFIABLE")

    def test_privacy_manifest_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            manifest = root / "Carvory" / "PrivacyInfo.xcprivacy"
            manifest.write_text("<plist></plist>\n", encoding="utf-8")

            report = Auditor(root, DEFAULT_POLICY).run().report()
            finding = next(finding for finding in report["findings"] if finding["id"] == "PRIV-002")
            self.assertEqual(finding["status"], "FAIL")
            self.assertIn("无需该文件", finding["expected"])
            self.assertEqual(finding["evidence"][0]["path"], "Carvory/PrivacyInfo.xcprivacy")

    def test_privacy_manifest_check_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            (root / "Carvory" / "PrivacyInfo.xcprivacy").write_text("<plist></plist>\n", encoding="utf-8")
            policy = dict(DEFAULT_POLICY)
            policy["forbid_privacy_manifest"] = False

            report = Auditor(root, policy).run().report(only_failures=False)
            finding = next(finding for finding in report["findings"] if finding["id"] == "PRIV-002")
            self.assertEqual(finding["status"], "NOT_VERIFIABLE")
            self.assertIn("检查已关闭", finding["title"])

    def test_sensitive_terms_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            (root / "StoreConfig" / "metadata.txt").write_text(
                "AI dating app with gambling features and explicit adult content.",
                encoding="utf-8",
            )
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            sensitive = [finding for finding in report["findings"] if finding["id"] == "SENSITIVE-001"]
            self.assertEqual(len(sensitive), 1)
            finding = sensitive[0]
            self.assertEqual(finding["status"], "FAIL")
            for label in ("AI/人工智能", "Dating/交友", "赌博/博彩", "色情/成人内容"):
                self.assertIn(label, finding["actual"])
            self.assertEqual(len(finding["evidence"]), 5)

    def test_sensitive_term_matching_does_not_match_inside_ascii_words(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            (root / "StoreConfig" / "metadata.txt").write_text(
                "main paid maintainable app copy",
                encoding="utf-8",
            )
            report = Auditor(root, DEFAULT_POLICY).run().report(only_failures=False)
            finding = next(finding for finding in report["findings"] if finding["id"] == "SENSITIVE-001")
            self.assertEqual(finding["status"], "PASS")

    def test_sensitive_terms_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ios-aside-review-test-") as directory:
            root = Path(directory)
            write_fixture(root)
            (root / "StoreConfig" / "metadata.txt").write_text("internal redflag", encoding="utf-8")
            policy = dict(DEFAULT_POLICY)
            policy["sensitive_terms"] = {"自定义": ["redflag"]}
            report = Auditor(root, policy).run().report(only_failures=False)
            finding = next(finding for finding in report["findings"] if finding["id"] == "SENSITIVE-001")
            self.assertIn("自定义", finding["actual"])
            self.assertIn("redflag", finding["actual"])


if __name__ == "__main__":
    unittest.main()
