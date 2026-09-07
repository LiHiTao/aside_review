#!/usr/bin/env python3
"""检查官方 GitHub 正式版本，在审计前安全更新整个技能目录。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import io

REPOSITORY = 'LiHiTao/aside_review'
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
ALLOWED_HOSTS = {'api.github.com', 'codeload.github.com'}


class UpdateError(RuntimeError):
    """无法确认最新版本或无法安全更新。"""


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise UpdateError(f'无效的正式版本号：{value!r}')
    return tuple(map(int, value.removeprefix('v').split('.')))


def _allowed_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.hostname not in ALLOWED_HOSTS or p.username or p.password or p.port not in (None, 443):
        raise UpdateError('更新地址必须属于官方 GitHub HTTPS 下载域名。')
    expected = '/repos/' + REPOSITORY + '/' if p.hostname == 'api.github.com' else '/' + REPOSITORY + '/'
    if not p.path.lower().startswith(expected.lower()):
        raise UpdateError('更新地址与已配置的官方仓库不一致。')


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _allowed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url):
    _allowed_url(url)
    opener = urllib.request.build_opener(_SafeRedirect())
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ios-aside-review-updater'})
    try:
        with opener.open(req, timeout=20) as response:
            _allowed_url(response.geturl())
            started = time.monotonic()
            chunks, size = [], 0
            while True:
                if time.monotonic() - started > 60:
                    raise UpdateError('下载超时。')
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise UpdateError('下载文件超过大小限制。')
                chunks.append(chunk)
            return b''.join(chunks)
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f'无法连接 GitHub 检查更新：{exc}') from exc


def _configuration(root):
    try:
        config = json.loads((root / 'skill-update.json').read_text(encoding='utf-8'))
        if not isinstance(config, dict) or config.get('repository') != REPOSITORY:
            raise UpdateError('更新仓库配置不匹配官方仓库。')
        return _version((root / 'VERSION').read_text(encoding='utf-8').strip())
    except (OSError, ValueError) as exc:
        raise UpdateError(f'无法读取技能版本配置：{exc}') from exc


def _extract(data, stage, expected_version):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 10000:
                raise UpdateError('更新压缩包文件数量无效。')
            total, prefix, seen = 0, None, set()
            for item in entries:
                path = PurePosixPath(item.filename)
                parts = path.parts
                if not parts or path.is_absolute() or '..' in parts or '\\' in item.filename or ':' in item.filename or any(part in ('', '.') for part in item.filename.rstrip('/').split('/')):
                    raise UpdateError('更新压缩包包含非法路径。')
                if prefix is None:
                    prefix = parts[0]
                if parts[0] != prefix or parts[0] in ('.', '..'):
                    raise UpdateError('更新压缩包根目录不唯一。')
                mode = item.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                    raise UpdateError('更新压缩包包含链接或不支持的文件。')
                total += item.file_size
                if total > MAX_EXPANDED or (item.file_size > 1024 * 1024 and item.file_size > max(item.compress_size, 1) * 300):
                    raise UpdateError('更新压缩包超过解压限制。')
                if len(parts) == 1:
                    if not item.is_dir():
                        raise UpdateError('更新压缩包缺少根目录。')
                    continue
                relative = PurePosixPath(*parts[1:])
                if relative.parts[0] == '.git' or str(relative).casefold() in seen:
                    raise UpdateError('更新压缩包包含 Git 数据或重复路径。')
                seen.add(str(relative).casefold())
                target = stage.joinpath(*relative.parts)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as source, target.open('xb') as dest:
                        shutil.copyfileobj(source, dest)
            if _configuration(stage) != expected_version:
                raise UpdateError('压缩包 VERSION 与 GitHub Release 版本不一致。')
            for name in ('SKILL.md', 'scripts/update_skill.py', 'scripts/audit_ios_a_side.py', 'scripts/render_audit_pdf.py', 'references/rules.md'):
                if not (stage / name).is_file():
                    raise UpdateError(f'更新压缩包缺少必需文件：{name}')
            for script in stage.rglob('*.py'):
                try:
                    compile(script.read_bytes(), str(script.relative_to(stage)), 'exec')
                except (SyntaxError, UnicodeError, ValueError) as exc:
                    raise UpdateError(f'更新压缩包包含无效 Python 脚本：{script.relative_to(stage)}：{exc}') from exc
            text = (stage / 'SKILL.md').read_text(encoding='utf-8')
            front = re.match(r'\A---\s*\n(.*?)\n---(?:\s|$)', text, re.S)
            if not front or not re.search(r'^name:\s*ios-aside-review\s*$', front.group(1), re.M):
                raise UpdateError('更新压缩包技能名称不匹配。')
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f'更新压缩包验证失败：{exc}') from exc


def ensure_latest(skill_root: Path) -> bool:
    """最新版返回 False；成功安装新版返回 True；失败阻止后续审计。"""
    root = Path(skill_root).resolve()
    lock = root.parent / ('.' + root.name + '.update.lock')
    locked = False
    stage = None
    try:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            locked = True
            with os.fdopen(fd, 'w') as handle:
                handle.write(str(os.getpid()))
        except FileExistsError as exc:
            raise UpdateError(f'另一更新进程正在运行；若进程已退出，请人工清理锁文件：{lock}') from exc
        current = _configuration(root)
        try:
            release = json.loads(_fetch(f'https://api.github.com/repos/{REPOSITORY}/releases/latest'))
        except (ValueError, UnicodeError) as exc:
            raise UpdateError('GitHub 版本响应不是有效 JSON。') from exc
        if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
            raise UpdateError('GitHub 未提供有效的正式 Release。')
        tag = release.get('tag_name')
        latest = _version(tag)
        if latest < current:
            raise UpdateError('GitHub 正式版本低于本地版本，已阻止降级；请核对版本发布状态。')
        if latest == current:
            return False
        if any((directory / '.git').exists() for directory in (root, *root.parents)):
            raise UpdateError('开发仓库或 worktree 存在新版，请先通过 Git 更新；自动更新不会覆盖开发目录。')
        data = _fetch(f'https://api.github.com/repos/{REPOSITORY}/zipball/{tag}')
        stage = Path(tempfile.mkdtemp(prefix='.' + root.name + '-stage-', dir=root.parent))
        _extract(data, stage, latest)
        backup = Path(tempfile.mkdtemp(prefix='.' + root.name + '-backup-', dir=root.parent))
        backup.rmdir()
        root.rename(backup)
        try:
            stage.rename(root)
            stage = None
        except OSError as exc:
            try:
                backup.rename(root)
            except OSError as rollback:
                raise UpdateError(f'更新及回滚失败，原技能保存在 {backup}：{rollback}') from exc
            raise UpdateError(f'更新失败，已恢复原技能：{exc}') from exc
        print(f'技能已更新，原目录完整备份：{backup}')
        return True
    except OSError as exc:
        raise UpdateError(f'技能更新失败：{exc}') from exc
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        if locked:
            lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='检查 GitHub 正式版本，必要时更新技能。')
    parser.add_argument('--check', action='store_true', help='检查并安装最新正式版本')
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        changed = ensure_latest(root)
        print(f"{'已安装最新技能' if changed else '技能已是最新版本'}：{(root / 'VERSION').read_text().strip()}")
        return 10 if changed else 0
    except UpdateError as exc:
        print(f'更新检查失败，停止执行：{exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
