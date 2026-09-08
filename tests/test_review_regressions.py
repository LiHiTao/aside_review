"""Regression tests through the full scanner, not only individual detectors."""
import json
from pathlib import Path
import plistlib
import tempfile
import unittest

from scripts.audit_ios_a_side import Auditor


SYNC_PROJECT = '''{ objectVersion = 77; rootObject = P; objects = {
                P = { isa = PBXProject; mainGroup = M; targets = (T,); };
                M = { isa = PBXGroup; children = (G,); sourceTree = "<group>"; };
                G = { isa = PBXFileSystemSynchronizedRootGroup; path = Saisons; sourceTree = "<group>"; exceptions = (E,); };
                E = { isa = PBXFileSystemSynchronizedBuildFileExceptionSet; target = T; membershipExceptions = (EXCLUSIONS); };
                T = { isa = PBXNativeTarget; buildConfigurationList = C; fileSystemSynchronizedGroups = (G,); buildPhases = (R,); };
                R = { isa = PBXResourcesBuildPhase; files = (); };
                C = { isa = XCConfigurationList; buildConfigurations = (D,); };
                D = { isa = XCBuildConfiguration; buildSettings = { INFOPLIST_FILE = Saisons/Info.plist; }; };
            }; }'''

class ReviewRegressionTests(unittest.TestCase):
    def test_agreement_does_not_create_restore_feature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Terms.swift"
            source.write_text('let terms = """\nThis app does not provide Restore Purchases.\nNo restorePurchases() or AppStore.sync() is offered.\n不含恢复购买功能。\n"""')
            finding = next(f for f in Auditor(root, {}).run().findings if f["id"] == "IAP-009")
            self.assertEqual(finding["status"], "PASS", finding)
            source.write_text(source.read_text() + '\nfunc restorePurchases() { try? await AppStore.sync() }')
            finding = next(f for f in Auditor(root, {}).run().findings if f["id"] == "IAP-009")
            self.assertEqual(finding["status"], "FAIL", finding)

    def test_localized_restore_control_is_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Localizable.xcstrings").write_text(json.dumps({"strings": {"restore": {"localizations": {"en": {"stringUnit": {"value": "Restore Purchases", "state": "translated"}}}}}}))
            finding = next(f for f in Auditor(root, {}).run().findings if f["id"] == "IAP-009")
            self.assertEqual(finding["status"], "FAIL", finding)

    def test_synced_launch_resources_and_exclusion_do_not_use_filename_shortcut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Saisons/Base.lproj").mkdir(parents=True)
            (root / "Saisons/Base.lproj/LaunchScreen.storyboard").write_text('<document launchScreen="YES"/>')
            (root / "Saisons/Info.plist").write_bytes(plistlib.dumps({"UILaunchStoryboardName": "LaunchScreen"}))
            (root / "Saisons.xcodeproj").mkdir()
            pbx = root / "Saisons.xcodeproj/project.pbxproj"
            template = SYNC_PROJECT
            for exclusions, expected in [("Info.plist,", "PASS"), ("Info.plist, Base.lproj/LaunchScreen.storyboard,", "NOT_VERIFIABLE")]:
                with self.subTest(exclusions=exclusions):
                    pbx.write_text(template.replace("EXCLUSIONS", exclusions))
                    finding = next(f for f in Auditor(root, {}).run().findings if f["id"] == "IOS-001")
                    self.assertEqual(finding["status"], expected, finding)
                    if expected != "PASS":
                        self.assertIn("排除", finding["actual"])

    def test_two_projects_resolve_their_own_same_named_storyboards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("Alpha", "Bravo"):
                (root / name / "Base.lproj").mkdir(parents=True)
                (root / name / "Base.lproj/LaunchScreen.storyboard").write_text('<document launchScreen="YES"/>')
                (root / name / "Info.plist").write_bytes(plistlib.dumps({"UILaunchStoryboardName": "LaunchScreen"}))
                project = root / (name + ".xcodeproj")
                project.mkdir()
                (project / "project.pbxproj").write_text(SYNC_PROJECT.replace("Saisons", name).replace("EXCLUSIONS", "Info.plist,"))
            findings = [f for f in Auditor(root, {}).run().findings if f["id"] == "IOS-001"]
            self.assertTrue(findings)
            self.assertTrue(all(f["status"] == "PASS" for f in findings), findings)
            # A stray traditional reference must not conceal exclusions in both actual targets.
            decoy = root / "Decoy.xcodeproj"
            decoy.mkdir()
            (decoy / "project.pbxproj").write_text("LaunchScreen.storyboard")
            for project in root.glob("[AB]*.xcodeproj/project.pbxproj"):
                project.write_text(project.read_text().replace("membershipExceptions = (Info.plist,)", "membershipExceptions = (Base.lproj/LaunchScreen.storyboard,)"))
            findings = [f for f in Auditor(root, {}).run().findings if f["id"] == "IOS-001"]
            self.assertTrue(all(f["status"] == "NOT_VERIFIABLE" for f in findings), findings)

    def test_dynamic_launch_name_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Info.plist").write_bytes(plistlib.dumps({"UILaunchStoryboardName": "$(PRODUCT_NAME)"}))
            (root / "LaunchScreen.storyboard").write_text('<document/>')
            finding = next(f for f in Auditor(root, {}).run().findings if f["id"] == "IOS-001")
            self.assertEqual(finding["status"], "NOT_VERIFIABLE", finding)
