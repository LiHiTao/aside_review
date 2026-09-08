import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.launch_membership import evaluate_membership, parse_project, ParseError


PROJECT = r'''// !$*UTF8*$!
{ objectVersion = 77; objects = {
 P = { isa = PBXProject; mainGroup = MAIN; targets = (APP, OTHER,); };
 MAIN = { isa = PBXGroup; children = (PARENT,); sourceTree = "<group>"; };
 PARENT = { isa = PBXGroup; path = "Source Files"; children = (SYNC,); sourceTree = "<group>"; };
 SYNC /* Saisons */ = { isa = PBXFileSystemSynchronizedRootGroup;
  exceptions = (EX,); explicitFolders = (); explicitFileTypes = {};
  path = Saisons; sourceTree = "<group>";
 };
 EX = { isa = PBXFileSystemSynchronizedBuildFileExceptionSet;
  target = APP; membershipExceptions = (Info.plist,);
 };
 APP = { isa = PBXNativeTarget; name = Saisons; buildConfigurationList = CFGS;
  fileSystemSynchronizedGroups = (SYNC,); buildPhases = (RES,);
 };
 OTHER = { isa = PBXNativeTarget; name = Other; buildConfigurationList = OCFGS; };
 RES = { isa = PBXResourcesBuildPhase; files = (); };
 CFGS = { isa = XCConfigurationList; buildConfigurations = (DEBUG,RELEASE,); };
 OCFGS = { isa = XCConfigurationList; buildConfigurations = (OTHERCFG,); };
 DEBUG = { isa = XCBuildConfiguration; name = Debug; buildSettings = {
  INFOPLIST_FILE = "$(SRCROOT)/Source Files/Saisons/Info.plist";
  INFOPLIST_KEY_UILaunchStoryboardName = LaunchScreen;
 }; };
 RELEASE = { isa = XCBuildConfiguration; name = Release; buildSettings = {
  INFOPLIST_FILE = "${SRCROOT}/Source Files/Saisons/Info.plist";
  INFOPLIST_KEY_UILaunchStoryboardName = LaunchScreen;
 }; };
 OTHERCFG = { isa = XCBuildConfiguration; buildSettings = { INFOPLIST_FILE = Other/Info.plist; }; };
 }; rootObject = P;
}'''


class LaunchMembershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'Saisons.xcodeproj' / 'project.pbxproj'
        self.project.parent.mkdir()
        self.candidate = self.root / 'Source Files/Saisons/Base.lproj/LaunchScreen.storyboard'
        self.candidate.parent.mkdir(parents=True)
        self.candidate.write_text('<document launchScreen="YES"/>')
        self.plist = self.candidate.parent.parent / 'Info.plist'
        self.plist.write_text('<plist/>')

    def check(self, raw=PROJECT, config=None):
        return evaluate_membership(self.project, raw, self.candidate, config or self.plist)[0]

    def test_synchronized_localized_storyboard_without_file_reference(self):
        self.assertTrue(self.check())
        self.assertNotIn('LaunchScreen.storyboard', PROJECT)

    def test_build_setting_configuration(self):
        self.assertTrue(self.check(config=self.project))

    def test_other_target_config_does_not_borrow_app_membership(self):
        self.assertFalse(self.check(config=self.root / 'Other/Info.plist'))

    def test_shared_plist_requires_both_targets_to_include_storyboard(self):
        raw = PROJECT.replace('Other/Info.plist', '"Source Files/Saisons/Info.plist"')
        self.assertFalse(self.check(raw))

    def test_group_is_not_automatically_bound_to_target(self):
        self.assertFalse(self.check(PROJECT.replace('fileSystemSynchronizedGroups = (SYNC,);', '')))

    def test_file_and_directory_exclusions(self):
        for excluded in ('Base.lproj/LaunchScreen.storyboard', 'Base.lproj', '"Base.lproj/"'):
            with self.subTest(excluded=excluded):
                self.assertFalse(self.check(PROJECT.replace('membershipExceptions = (Info.plist,);', f'membershipExceptions = ({excluded},);')))

    def test_other_target_exclusion_does_not_exclude_app(self):
        raw = PROJECT.replace('target = APP;', 'target = OTHER;').replace('membershipExceptions = (Info.plist,);', 'membershipExceptions = (Base.lproj/LaunchScreen.storyboard,);')
        self.assertTrue(self.check(raw))

    def test_source_root_path(self):
        raw = PROJECT.replace('path = Saisons; sourceTree = "<group>";', 'path = "Source Files/Saisons"; sourceTree = SOURCE_ROOT;')
        self.assertTrue(self.check(raw))

    def test_absolute_path(self):
        raw = PROJECT.replace('path = Saisons; sourceTree = "<group>";', f'path = "{self.plist.parent}"; sourceTree = "<absolute>";')
        self.assertTrue(self.check(raw))

    def test_unknown_source_tree(self):
        self.assertFalse(self.check(PROJECT.replace('path = Saisons; sourceTree = "<group>";', 'path = Saisons; sourceTree = SDKROOT;')))

    def test_group_outside_parent_does_not_match(self):
        self.assertFalse(self.check(PROJECT.replace('path = "Source Files";', 'path = Elsewhere;')))

    def test_ambiguous_parent(self):
        self.assertFalse(self.check(PROJECT.replace('children = (PARENT,);', 'children = (PARENT,SYNC,);')))

    def test_build_exclusion(self):
        self.assertFalse(self.check(PROJECT.replace('name = Release; buildSettings = {', 'name = Release; buildSettings = { EXCLUDED_SOURCE_FILE_NAMES = "*.storyboard";')))

    def test_explicit_folder_copy_is_unproven(self):
        self.assertFalse(self.check(PROJECT.replace('explicitFolders = ();', 'explicitFolders = (Base.lproj,);')))

    def test_unknown_exception_and_unresolved_reference(self):
        self.assertFalse(self.check(PROJECT.replace('PBXFileSystemSynchronizedBuildFileExceptionSet', 'FutureExceptionSet')))
        self.assertFalse(self.check(PROJECT.replace('exceptions = (EX,);', 'exceptions = (MISSING,);')))

    def test_build_phase_exception(self):
        raw = PROJECT.replace('PBXFileSystemSynchronizedBuildFileExceptionSet', 'PBXFileSystemSynchronizedGroupBuildPhaseMembershipExceptionSet').replace('target = APP;', 'buildPhase = RES;').replace('membershipExceptions = (Info.plist,);', 'membershipExceptions = (Base.lproj,);')
        self.assertFalse(self.check(raw))

    def traditional_project(self, variant=False):
        raw = PROJECT.replace('fileSystemSynchronizedGroups = (SYNC,);', '')
        raw = raw.replace('children = (SYNC,);', 'children = (SYNC,OLD,);')
        if variant:
            objects = 'OLD = { isa = PBXGroup; path = Saisons; children = (VG,); sourceTree = "<group>"; }; VG = { isa = PBXVariantGroup; name = LaunchScreen.storyboard; children = (FILE,); sourceTree = "<group>"; };'
        else:
            objects = 'OLD = { isa = PBXGroup; path = Saisons; children = (FILE,); sourceTree = "<group>"; };'
        objects += ' FILE = { isa = PBXFileReference; path = Base.lproj/LaunchScreen.storyboard; sourceTree = "<group>"; lastKnownFileType = file.storyboard; };'
        objects += ' BUILD = { isa = PBXBuildFile; fileRef = ' + ('VG' if variant else 'FILE') + '; };'
        raw = raw.replace(' }; rootObject = P;', objects + ' }; rootObject = P;')
        return raw.replace('isa = PBXResourcesBuildPhase; files = ();', 'isa = PBXResourcesBuildPhase; files = (BUILD,);')

    def test_mixed_project_traditional_resources(self):
        self.assertTrue(self.check(self.traditional_project()))
        self.assertTrue(self.check(self.traditional_project(variant=True)))

    def test_mixed_project_resources_of_another_target_do_not_count(self):
        raw = self.traditional_project().replace('buildPhases = (RES,);', '')
        raw = raw.replace('name = Other;', 'name = Other; buildPhases = (RES,);')
        self.assertFalse(self.check(raw))

    def test_mixed_project_unbuilt_file_reference_does_not_count(self):
        self.assertFalse(self.check(self.traditional_project().replace('files = (BUILD,);', 'files = ();')))

    def test_mixed_project_wrong_file_path_does_not_count(self):
        self.assertFalse(self.check(self.traditional_project().replace('path = Base.lproj/LaunchScreen.storyboard;', 'path = Other/LaunchScreen.storyboard;')))

    def test_project_level_resource_exclusion(self):
        raw = PROJECT.replace('mainGroup = MAIN;', 'mainGroup = MAIN; buildConfigurationList = PCFGS;')
        raw = raw.replace(' }; rootObject = P;', ' PCFGS = { buildConfigurations = (PCFG,); }; PCFG = { buildSettings = { EXCLUDED_SOURCE_FILE_NAMES = "*.storyboard"; }; }; }; rootObject = P;')
        self.assertFalse(self.check(raw))

    def test_launch_build_setting_overrides_plist(self):
        self.assertFalse(self.check(PROJECT.replace('INFOPLIST_KEY_UILaunchStoryboardName = LaunchScreen;', 'INFOPLIST_KEY_UILaunchStoryboardName = OtherLaunch;')))

    def test_malformed_project_is_unproven(self):
        self.assertFalse(self.check(PROJECT[:-3]))
        self.assertFalse(self.check(PROJECT + ' /* incomplete'))

    def test_quoted_punctuation_comment_unicode_and_duplicate_keys(self):
        data = parse_project(r'{ path = "A; // B\U0020\"C\""; /* x */ items = (one, two); }')
        self.assertEqual(data['path'], 'A; // B "C"')
        self.assertEqual(data['items'], ['one', 'two'])
        with self.assertRaises(ParseError):
            parse_project('{ x = 1; x = 2; }')


if __name__ == '__main__':
    unittest.main()
