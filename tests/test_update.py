"""完全离线的技能更新与失败保护测试。"""
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_skill as updater


def package(version='1.1.0', extra=None, repository=updater.REPOSITORY):
    files = {
        'VERSION': version,
        'skill-update.json': json.dumps({'repository': repository}),
        'SKILL.md': '---\nname: ios-aside-review\ndescription: Test\n---\n',
        'scripts/update_skill.py': '# updater',
        'scripts/audit_ios_a_side.py': '# scanner',
        'scripts/render_audit_pdf.py': '# pdf',
        'references/rules.md': '# rules',
    }
    files.update(extra or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name, content in files.items():
            archive.writestr('repo-tag/' + name, content)
    return out.getvalue()


def release(tag='v1.1.0', **kwargs):
    return json.dumps(dict(tag_name=tag, draft=False, prerelease=False, **kwargs)).encode()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / 'ios-aside-review'
        self.root.mkdir()
        (self.root / 'VERSION').write_text('1.0.0')
        (self.root / 'skill-update.json').write_text(json.dumps({'repository': updater.REPOSITORY}))
        (self.root / 'local-note.txt').write_text('preserve me')

    def assert_original(self):
        self.assertEqual((self.root / 'VERSION').read_text(), '1.0.0')
        self.assertEqual((self.root / 'local-note.txt').read_text(), 'preserve me')
        self.assertFalse((self.parent / '.ios-aside-review.update.lock').exists())

    def test_upgrade_preserves_backup(self):
        with patch.object(updater, '_fetch', side_effect=[release(), package()]):
            self.assertTrue(updater.ensure_latest(self.root))
        self.assertEqual((self.root / 'VERSION').read_text(), '1.1.0')
        backups = list(self.parent.glob('.ios-aside-review-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'local-note.txt').read_text(), 'preserve me')

    def test_same_version_only_requests_metadata(self):
        with patch.object(updater, '_fetch', return_value=release('v1.0.0')) as fetch:
            self.assertFalse(updater.ensure_latest(self.root))
            self.assertEqual(fetch.call_count, 1)
        self.assert_original()

    def test_offline_stops(self):
        with patch.object(updater, '_fetch', side_effect=updater.UpdateError('offline')):
            with self.assertRaises(updater.UpdateError):
                updater.ensure_latest(self.root)
        self.assert_original()

    def test_bad_archives_never_replace_original(self):
        for data in (b'not zip', package(extra={'../../outside': 'bad'}), package(extra={'/absolute': 'bad'}), package(version='1.9.9'), package(repository='other/repo'), package(extra={'SKILL.md': '---\nname: wrong\n---'})):
            with self.subTest(data=data[:20]), patch.object(updater, '_fetch', side_effect=[release(), data]):
                with self.assertRaises(updater.UpdateError):
                    updater.ensure_latest(self.root)
                self.assert_original()

    def test_symlink_rejected(self):
        output = io.BytesIO(package())
        with zipfile.ZipFile(output, 'a') as archive:
            item = zipfile.ZipInfo('repo-tag/link')
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(item, '/etc/passwd')
        with patch.object(updater, '_fetch', side_effect=[release(), output.getvalue()]):
            with self.assertRaises(updater.UpdateError):
                updater.ensure_latest(self.root)
        self.assert_original()

    def test_rename_failure_rolls_back(self):
        real_rename = Path.rename
        def fail_stage(path, target):
            if '-stage-' in path.name:
                raise OSError('simulated failure')
            return real_rename(path, target)
        with patch.object(updater, '_fetch', side_effect=[release(), package()]), patch.object(Path, 'rename', fail_stage):
            with self.assertRaisesRegex(updater.UpdateError, '已恢复'):
                updater.ensure_latest(self.root)
        self.assert_original()

    def test_downgrade_rejected(self):
        with patch.object(updater, '_fetch', return_value=release('v0.9.0')):
            with self.assertRaisesRegex(updater.UpdateError, '降级'):
                updater.ensure_latest(self.root)
        self.assert_original()

    def test_development_git_blocks_upgrade_but_not_same(self):
        (self.root / '.git').write_text('gitdir: elsewhere')
        with patch.object(updater, '_fetch', return_value=release()):
            with self.assertRaisesRegex(updater.UpdateError, '开发'):
                updater.ensure_latest(self.root)
        with patch.object(updater, '_fetch', return_value=release('v1.0.0')):
            self.assertFalse(updater.ensure_latest(self.root))
        self.assert_original()

    def test_nested_git_repository_blocks_upgrade(self):
        (self.parent / '.git').mkdir()
        with patch.object(updater, '_fetch', return_value=release()) as fetch:
            with self.assertRaisesRegex(updater.UpdateError, '开发'):
                updater.ensure_latest(self.root)
            self.assertEqual(fetch.call_count, 1)
        self.assert_original()

    def test_invalid_python_release_preserves_installation(self):
        archive = package(extra={'scripts/broken.py': 'def broken(:\n'})
        with patch.object(updater, '_fetch', side_effect=[release(), archive]):
            with self.assertRaisesRegex(updater.UpdateError, '无效 Python'):
                updater.ensure_latest(self.root)
        self.assert_original()

    def test_release_validation(self):
        bad = [b'bad', b'[]', release('v01.0.0'), release('v1.2.3-beta')]
        for key in ('draft', 'prerelease'):
            value = json.loads(release())
            value[key] = True
            bad.append(json.dumps(value).encode())
        for value in bad:
            with self.subTest(value=value), patch.object(updater, '_fetch', return_value=value):
                with self.assertRaises(updater.UpdateError):
                    updater.ensure_latest(self.root)
                self.assert_original()

    def test_lock_blocks_concurrent_update(self):
        lock = self.parent / '.ios-aside-review.update.lock'
        lock.write_text('other process')
        with patch.object(updater, '_fetch') as fetch:
            with self.assertRaisesRegex(updater.UpdateError, '另一更新'):
                updater.ensure_latest(self.root)
            fetch.assert_not_called()
        self.assertEqual(lock.read_text(), 'other process')

    def test_url_restricted_to_official_repo(self):
        for url in ('http://api.github.com/repos/LiHiTao/aside_review/releases/latest', 'https://evil.example/LiHiTao/aside_review/', 'https://api.github.com/repos/other/repo/releases/latest', 'https://codeload.github.com/other/repo/zip/v1.0.0', 'https://user@api.github.com/repos/LiHiTao/aside_review/releases/latest'):
            with self.subTest(url=url), self.assertRaises(updater.UpdateError):
                updater._allowed_url(url)
        updater._allowed_url('https://codeload.github.com/LiHiTao/aside_review/legacy.zip/refs/tags/v1.0.0')

    def test_expansion_limit_rejected(self):
        with patch.object(updater, '_fetch', side_effect=[release(), package()]), patch.object(updater, 'MAX_EXPANDED', 10):
            with self.assertRaisesRegex(updater.UpdateError, '解压限制'):
                updater.ensure_latest(self.root)
        self.assert_original()


if __name__ == '__main__':
    unittest.main()
