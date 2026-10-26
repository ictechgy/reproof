"""Prepare an opt-in, private mirror wrapper; never execute a build or network call."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit


class MirrorError(ValueError):
    """Fixed public diagnostics only; never include operator configuration values."""


PINS = {
    'build.gradle.kts': 'b4690997f64b65b674a1b07fe2ed9048a764dedd63665ce0c01b74116602a7d9',
    'settings.gradle.kts': 'a2298cdb84b0cbeb449f45eb011d0336298ad8eaedb09e7683c55eca127deff2',
    'gradlew': 'b187b4c52e749f5760afdd6fadc31b2a98ad35fb249bf0dff03b72650f320409',
    'gradlew.bat': '1d297e00bd21de3ace22b4d7f2de1f9dfa858883d66bbf7c1ccbecccec8f4f3b',
    'gradle/wrapper/gradle-wrapper.jar': '7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172',
    'gradle/wrapper/gradle-wrapper.properties': '5f23b090b2d8ba9b2affdacb735a892fe95e906ed05669a562d390fa6c3c67a7',
}


def _require(ok, code):
    if not ok:
        raise MirrorError(code)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _no_links(path):
    path = Path(os.path.abspath(path))
    for component in [*reversed(path.parents), path]:
        _require(not component.is_symlink(), 'mirror_path_invalid')
    return path


def _read(path, limit):
    path = _no_links(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                     and before.st_size <= limit, 'mirror_input_invalid')
            data = source.read(limit + 1)
            after = os.fstat(source.fileno())
            _require(len(data) <= limit and len(data) == before.st_size
                     and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                     == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                     'mirror_input_changed')
            return data
    except OSError:
        raise MirrorError('mirror_input_unavailable') from None


def _url(value):
    # Restricted ASCII grammar also makes Groovy single-quoted literals and Java
    # property values safe. No percent escapes, credentials, query or fragments.
    _require(type(value) is str and 1 <= len(value) <= 2048
             and re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[A-Za-z0-9_./~-]*', value)
             is not None, 'mirror_url_invalid')
    try:
        parsed = urlsplit(value)
        _require(parsed.hostname is not None and parsed.port != 0
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment,
                 'mirror_url_invalid')
        host = parsed.hostname
        _require(all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', label)
                     for label in host.split('.'))
                 and all(part not in {'.', '..'} for part in parsed.path.split('/')),
                 'mirror_url_invalid')
    except ValueError:
        raise MirrorError('mirror_url_invalid') from None
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'mirror_config_invalid')
        result[key] = value
    return result


def parse_config(data):
    try:
        config = json.loads(data, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError):
        raise MirrorError('mirror_config_invalid') from None
    _require(type(config) is dict and set(config) == {
        'schemaVersion', 'pluginRepositories', 'dependencyRepositories', 'distributionUrl'
    } and type(config['schemaVersion']) is int and config['schemaVersion'] == 1,
             'mirror_config_invalid')
    result = {'schemaVersion': 1}
    for key in ('pluginRepositories', 'dependencyRepositories'):
        values = config[key]
        _require(type(values) is list and 1 <= len(values) <= 8, 'mirror_config_invalid')
        result[key] = [_url(value) for value in values]
        _require(len(set(result[key])) == len(values), 'mirror_config_invalid')
    result['distributionUrl'] = _url(config['distributionUrl'])
    _require(result['distributionUrl'].endswith('/gradle-8.14.5-bin.zip'),
             'mirror_distribution_invalid')
    return result


def render_init(config):
    # Only reviewed Reproof settings are supported, not arbitrary user builds.
    lines = [
        '// Generated local configuration; contains private operator URLs. Do not upload.',
        'gradle.beforeSettings { settings ->',
        '    def settingsFile = new File(settings.settingsDir, "settings.gradle.kts")',
        '    def digest = java.security.MessageDigest.getInstance("SHA-256")',
        '        .digest(settingsFile.bytes).encodeHex().toString()',
        f'    if (digest != "{PINS["settings.gradle.kts"]}") {{',
        '        throw new GradleException("reproof_mirror_settings_changed")',
        '    }',
        '    def buildFile = new File(settings.settingsDir, "build.gradle.kts")',
        '    def buildDigest = java.security.MessageDigest.getInstance("SHA-256")',
        '        .digest(buildFile.bytes).encodeHex().toString()',
        f'    if (buildDigest != "{PINS["build.gradle.kts"]}") {{',
        '        throw new GradleException("reproof_mirror_build_changed")',
        '    }',
        '}',
        'gradle.settingsEvaluated { settings ->',
        '    if (settings.dependencyResolutionManagement.repositoriesMode.get() !=',
        '        org.gradle.api.initialization.resolve.RepositoriesMode.FAIL_ON_PROJECT_REPOS) {',
        '        throw new GradleException("reproof_mirror_repository_mode_invalid")',
        '    }',
    ]
    for scope, key in [('settings.pluginManagement', 'pluginRepositories'),
                       ('settings.dependencyResolutionManagement', 'dependencyRepositories')]:
        lines.append(f'    {scope}.repositories.clear()')
        for index, url in enumerate(config[key]):
            lines += [f'    {scope}.repositories.maven {{',
                      f"        name = 'reproofMirror{index}'",
                      f"        url = new java.net.URI('{url}')",
                      '        allowInsecureProtocol = false', '    }']
    lines += ['}', 'gradle.beforeProject { project ->',
              '    def repositories = project.buildscript.repositories',
              '    repositories.clear()']
    for index, url in enumerate(config['pluginRepositories']):
        lines += ['    repositories.maven {',
                  f"        name = 'reproofBuildscriptMirror{index}'",
                  f"        url = new java.net.URI('{url}')",
                  '        allowInsecureProtocol = false', '    }']
    lines += [
        '    def approved = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap())',
        '    approved.addAll(repositories)',
        '    repositories.whenObjectAdded { repository ->',
        '        if (!approved.contains(repository)) { repositories.remove(repository) }',
        '    }', '}', '']
    return '\n'.join(lines).encode()


def prepare(android_root, config_path, output):
    root = _no_links(android_root)
    destination = _no_links(output)
    _require(root.is_dir() and destination.parent.is_dir() and not destination.exists(),
             'mirror_output_invalid')
    _require(not destination.is_relative_to(root), 'mirror_output_invalid')
    parent_info = destination.parent.stat()
    _require(parent_info.st_uid == os.getuid() and not parent_info.st_mode & 0o022,
             'mirror_output_parent_unsafe')
    config = parse_config(_read(config_path, 32 * 1024))
    sources = {}
    for name, digest in PINS.items():
        sources[name] = _read(root / name, 128 * 1024)
        _require(_digest(sources[name]) == digest, 'mirror_source_changed')
    properties = sources['gradle/wrapper/gradle-wrapper.properties'].decode('ascii')
    properties = '\n'.join(
        'distributionUrl=' + config['distributionUrl'].replace(':', '\\:')
        if line.startswith('distributionUrl=') else line
        for line in properties.splitlines()) + '\n'
    files = {name: data for name, data in sources.items() if name not in {'settings.gradle.kts', 'build.gradle.kts'}}
    files['gradle/wrapper/gradle-wrapper.properties'] = properties.encode('ascii')
    files['mirrors.init.gradle'] = render_init(config)
    manifest = {'schemaVersion': 1, 'kind': 'reproof-local-mirror-bundle',
                'sourcePins': PINS, 'files': {name: {'sha256': _digest(data),
                'bytes': len(data), 'mode': '0700' if name == 'gradlew' else '0600'}
                for name, data in sorted(files.items())}, 'executed': False}
    files['manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    try:
        destination.mkdir(mode=0o700)
        (destination / 'gradle').mkdir(mode=0o700)
        (destination / 'gradle/wrapper').mkdir(mode=0o700)
        for name, data in files.items():
            mode = 0o700 if name == 'gradlew' else 0o600
            fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, 'wb') as target:
                os.fchmod(target.fileno(), mode)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
        # A partial bundle has no READY marker; do not execute one.
        fd = os.open(destination / 'READY', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as marker:
            marker.write(b'prepared-not-executed\n')
    except OSError:
        raise MirrorError('mirror_output_incomplete') from None
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--android-root', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        prepare(args.android_root, args.config, args.output)
    except MirrorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print('mirror_preparation_failed', file=sys.stderr)
        return 2
    print('mirror_bundle_prepared_not_executed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
