"""Conservative static membership evidence for Xcode synchronized folders.

The object relationships follow Xcodeproj's PBXFileSystemSynchronizedRootGroup
and PBXFileSystemSynchronizedBuildFileExceptionSet models. No build is run.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
import re


class ParseError(ValueError):
    pass


def parse_project(raw: str) -> dict:
    """Parse the OpenStep plist subset emitted by Xcode, including comments."""
    tokens = []
    cursor = 0
    while cursor < len(raw):
        if raw[cursor].isspace() or raw[cursor] == '\ufeff':
            cursor += 1
        elif raw.startswith('//', cursor):
            end = raw.find('\n', cursor)
            cursor = len(raw) if end < 0 else end + 1
        elif raw.startswith('/*', cursor):
            end = raw.find('*/', cursor + 2)
            if end < 0:
                raise ParseError('unterminated comment')
            cursor = end + 2
        elif raw[cursor] == '"':
            cursor += 1
            value = ''
            while cursor < len(raw) and raw[cursor] != '"':
                char = raw[cursor]
                cursor += 1
                if char == '\\':
                    if cursor >= len(raw):
                        raise ParseError('unterminated escape')
                    char = raw[cursor]
                    cursor += 1
                    if char in '01234567':
                        digits = char
                        while cursor < len(raw) and len(digits) < 3 and raw[cursor] in '01234567':
                            digits += raw[cursor]
                            cursor += 1
                        char = chr(int(digits, 8))
                    elif char == 'U':
                        digits = raw[cursor:cursor + 4]
                        if not re.fullmatch(r'[0-9a-fA-F]{4}', digits):
                            raise ParseError('invalid unicode escape')
                        char = chr(int(digits, 16))
                        cursor += 4
                    else:
                        char = {'n': '\n', 'r': '\r', 't': '\t'}.get(char, char)
                value += char
            if cursor >= len(raw):
                raise ParseError('unterminated string')
            tokens.append(('value', value))
            cursor += 1
        elif raw[cursor] in '{}()=;,':
            tokens.append((raw[cursor], raw[cursor]))
            cursor += 1
        else:
            start = cursor
            while cursor < len(raw) and not raw[cursor].isspace() and raw[cursor] not in '{}()=;,"':
                if raw.startswith(('//', '/*'), cursor):
                    break
                cursor += 1
            if cursor == start:
                raise ParseError('invalid token')
            tokens.append(('value', raw[start:cursor]))
    index = 0

    def take(kind):
        nonlocal index
        if index >= len(tokens) or tokens[index][0] != kind:
            raise ParseError('unexpected token')
        value = tokens[index][1]
        index += 1
        return value

    def value():
        nonlocal index
        if index >= len(tokens):
            raise ParseError('missing value')
        if tokens[index][0] == '{':
            take('{')
            result = {}
            while index < len(tokens) and tokens[index][0] != '}':
                key = take('value')
                take('=')
                if key in result:
                    raise ParseError('duplicate key')
                result[key] = value()
                take(';')
            take('}')
            return result
        if tokens[index][0] == '(':
            take('(')
            result = []
            while index < len(tokens) and tokens[index][0] != ')':
                result.append(value())
                if index < len(tokens) and tokens[index][0] == ',':
                    take(',')
                elif index >= len(tokens) or tokens[index][0] != ')':
                    raise ParseError('missing comma')
            take(')')
            return result
        return take('value')

    result = value()
    if index != len(tokens) or not isinstance(result, dict):
        raise ParseError('invalid project root')
    return result


def evaluate_membership(project_path: Path, raw: str, candidate: Path, config_path: Path) -> tuple[bool, str]:
    """Confirm all identifiable launch-config targets include this storyboard.

    Paths may be absolute or relative; project_path is project.pbxproj. Unknown
    relationships or formats return false, never a guessed target membership.
    """
    try:
        return _evaluate(project_path.resolve(), raw, candidate.resolve(), config_path.resolve())
    except (ParseError, KeyError, TypeError, ValueError, AttributeError, OSError, RecursionError):
        return False, '同步目录工程结构或路径无法静态解析'


def _evaluate(project_path: Path, raw: str, candidate: Path, config_path: Path) -> tuple[bool, str]:
    project = parse_project(raw)
    objects = project['objects']
    root = objects[project['rootObject']]
    if root.get('isa') != 'PBXProject' or not candidate.is_file():
        return False, '缺少有效工程或 storyboard 文件'
    base = project_path.parent.parent
    project_dir = root.get('projectDirPath', '')
    if project_dir:
        if '$' in project_dir:
            return False, '工程根路径包含未解析变量'
        base = (base / project_dir).resolve()
    parents = {}
    for key, obj in objects.items():
        if obj.get('isa') in ('PBXGroup', 'PBXVariantGroup'):
            for child in obj.get('children', []):
                parents.setdefault(child, []).append(key)

    def path_for(key, seen=()):
        if key in seen:
            raise ParseError('group cycle')
        obj = objects[key]
        path = obj.get('path', '')
        if not isinstance(path, str) or '$' in path:
            raise ParseError('dynamic path')
        source = obj.get('sourceTree', '<group>')
        if source in ('SOURCE_ROOT', 'PROJECT_DIR'):
            anchor = base
        elif source == '<absolute>':
            if not Path(path).is_absolute():
                raise ParseError('nonabsolute path')
            anchor = Path('/')
        elif source == '<group>':
            if key == root.get('mainGroup'):
                anchor = base
            else:
                owners = parents.get(key, [])
                if len(owners) != 1:
                    raise ParseError('ambiguous group parent')
                anchor = path_for(owners[0], seen + (key,))
        else:
            raise ParseError('unsupported source tree')
        return (anchor / path).resolve()

    def setting_path(value):
        if not isinstance(value, str):
            return None
        for macro in ('SRCROOT', 'SOURCE_ROOT', 'PROJECT_DIR'):
            value = value.replace('$(' + macro + ')', str(base)).replace('${' + macro + '}', str(base))
        if '$' in value:
            return None
        return (base / value).resolve()

    def explicit_resource_member(target):
        # Mixed projects may keep a traditional Resources phase for one target
        # while using synchronized folders in another. Follow actual IDs, not
        # labels/comments or any matching filename elsewhere in the project.
        for phase_id in target.get('buildPhases', []):
            phase = objects[phase_id]
            if phase.get('isa') != 'PBXResourcesBuildPhase':
                continue
            for build_id in phase.get('files', []):
                build_file = objects[build_id]
                if build_file.get('isa') != 'PBXBuildFile':
                    continue
                # A platform-restricted reference alone is insufficient for an
                # unqualified static target membership claim.
                if build_file.get('platformFilter') or build_file.get('platformFilters'):
                    continue
                reference_id = build_file.get('fileRef')
                if not reference_id:
                    continue
                reference = objects[reference_id]
                references = reference.get('children', []) if reference.get('isa') == 'PBXVariantGroup' else [reference_id]
                for child_id in references:
                    child = objects[child_id]
                    if child.get('isa') != 'PBXFileReference':
                        continue
                    file_type = child.get('explicitFileType', child.get('lastKnownFileType'))
                    if file_type and file_type != 'file.storyboard':
                        continue
                    if path_for(child_id) == candidate:
                        return True
        return False

    targets = []
    for target_id in root.get('targets', []):
        target = objects[target_id]
        if target.get('isa') != 'PBXNativeTarget':
            continue
        configurations = objects[target['buildConfigurationList']]['buildConfigurations']
        matching = []
        for configuration_id in configurations:
            config = objects[configuration_id]
            settings = config.get('buildSettings', {})
            if config_path == project_path:
                matches = settings.get('INFOPLIST_KEY_UILaunchStoryboardName') in (candidate.stem, candidate.name)
            else:
                matches = setting_path(settings.get('INFOPLIST_FILE')) == config_path
            if matches:
                matching.append(config)
        if matching:
            targets.append((target_id, target, matching))
    if not targets:
        return False, '无法将启动页配置关联到明确的 target'

    project_configs = []
    if root.get('buildConfigurationList'):
        project_configs = [objects[key] for key in objects[root['buildConfigurationList']]['buildConfigurations']]
    for target_id, target, configs in targets:
        # Project-level exclusions and external configuration inheritance can
        # affect target resources as well. Inspect them conservatively.
        for config in configs + project_configs:
            settings = config.get('buildSettings', {})
            launch_override = settings.get('INFOPLIST_KEY_UILaunchStoryboardName')
            if config in configs and launch_override and launch_override not in (candidate.stem, candidate.name):
                return False, 'target 构建设置指定了不同的启动 storyboard'
            if config.get('baseConfigurationReference'):
                return False, 'target 引用外部 xcconfig，需核实资源排除设置'
            for key, patterns in settings.items():
                if key.startswith('EXCLUDED_SOURCE_FILE_NAMES'):
                    if isinstance(patterns, str):
                        patterns = patterns.split()
                    if not isinstance(patterns, list) or any('$' in pattern for pattern in patterns):
                        return False, '资源排除规则无法解析'
                    if any(fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(str(candidate), pattern) for pattern in patterns):
                        return False, '构建设置排除了启动 storyboard'
        included = explicit_resource_member(target)
        for group_id in target.get('fileSystemSynchronizedGroups', []):
            group = objects[group_id]
            if group.get('isa') != 'PBXFileSystemSynchronizedRootGroup':
                continue
            group_path = path_for(group_id)
            try:
                relative = candidate.relative_to(group_path).as_posix()
            except ValueError:
                continue
            # Explicit folders are copied as opaque folder resources. They do
            # not establish a compiled launch storyboard at the bundle root.
            for folder in group.get('explicitFolders', []):
                if _covers(folder, relative):
                    return False, '启动页位于按整体复制的显式文件夹中'
            if relative in group.get('explicitFileTypes', {}):
                return False, '启动页文件类型被显式覆盖，需复核编译类型'
            for exception_id in group.get('exceptions', []):
                exception = objects[exception_id]
                kind = exception.get('isa')
                if kind == 'PBXFileSystemSynchronizedBuildFileExceptionSet':
                    if 'target' not in exception:
                        return False, '同步目录例外缺少 target 关系'
                    relevant = exception['target'] == target_id
                elif kind == 'PBXFileSystemSynchronizedGroupBuildPhaseMembershipExceptionSet':
                    phase = exception.get('buildPhase')
                    if not phase or phase not in objects:
                        return False, '同步目录例外缺少构建阶段关系'
                    relevant = phase in target.get('buildPhases', [])
                else:
                    return False, '同步目录包含尚未支持的例外类型'
                if relevant:
                    exclusions = exception.get('membershipExceptions', [])
                    if not isinstance(exclusions, list):
                        return False, '同步目录排除列表格式无法解析'
                    for excluded in exclusions:
                        if _covers(excluded, relative):
                            return False, '目标同步目录的 membershipExceptions 排除了启动页或其父目录'
            included = True
        if not included:
            return False, '启动页未关联该 target 的资源阶段或同步目录'
    return True, '启动页配置已关联 target，文件已加入其资源阶段或同步目录且未被排除（静态证据）'


def _covers(entry: str, relative: str) -> bool:
    if not isinstance(entry, str) or '$' in entry or '..' in PurePosixPath(entry).parts or entry.startswith('/'):
        raise ParseError('unsupported exception path')
    normalized = PurePosixPath(entry).as_posix().rstrip('/')
    return normalized in ('', '.') or relative == normalized or relative.startswith(normalized + '/')
