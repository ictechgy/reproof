"""Build selected Python modules and declared public runtime assets."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import tempfile

from setuptools import setup
from setuptools.command.build_py import build_py

resources = runpy.run_path('reproloop/resources.py')
checked_resource_files = resources['checked_resource_files']
freeze_resource_files = resources['freeze_resource_files']
read_public_source = resources['_read']


def check_asset_cache(target, names, *, manifest=True):
    for parent in (target, *target.parents):
        if parent.is_symlink():
            raise RuntimeError('Use a clean build directory for runtime resources')
    if not target.exists() and not target.is_symlink(): return
    if target.is_symlink() or not target.is_dir():
        raise RuntimeError('Use a clean build directory for runtime resources')
    allowed = set(names) | ({'manifest.json'} if manifest else set())
    directories = {parent.as_posix() for name in names for parent in Path(name).parents
                   if parent != Path('.')}
    for path in target.rglob('*'):
        info = path.lstat(); name = path.relative_to(target).as_posix()
        valid = ((stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and name in allowed)
                 or (stat.S_ISDIR(info.st_mode) and name in directories))
        if not valid:
            raise RuntimeError('Unlisted or unsafe cached asset; use a clean build directory')


def write_asset(destination, data):
    descriptor, temporary = tempfile.mkstemp(prefix='.repro-resource-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream: stream.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


class RuntimeAssetBuild(build_py):
    def get_source_files(self):
        return [*super().get_source_files(), 'distribution-resources.json',
                *checked_resource_files(Path.cwd())]

    def run(self):
        if self.editable_mode:
            super().run()
            return
        source = Path.cwd()
        build_root = Path(self.build_lib)
        target = build_root / 'reproloop' / '_assets'
        frozen = freeze_resource_files(source); names = list(frozen)
        modules = {}
        for package, module, filename in self.find_all_modules():
            selected = Path(filename).absolute().relative_to(source).as_posix()
            relative = '/'.join([*package.split('.'), module + '.py'])
            modules[relative] = read_public_source(source, selected)
        allowed = [*modules, 'reproloop/_assets/manifest.json',
                   *('reproloop/_assets/' + name for name in names)]
        check_asset_cache(build_root, allowed, manifest=False)
        self.compile = False
        self.optimize = 0
        super().run()
        # Module code has the same source-of-truth rule as data assets. Neither
        # a newer build cache nor removed modules may replace the selected code.
        for name, raw in modules.items():
            destination = build_root / name
            self.mkpath(str(destination.parent))
            write_asset(destination, raw)
        hashes = {}
        for name, raw in frozen.items():
            destination = target / name
            self.mkpath(str(destination.parent))
            write_asset(destination, raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        manifest = {'schemaVersion': 1, 'files': names, 'sha256': hashes}
        write_asset(target / 'manifest.json', (json.dumps(manifest, sort_keys=True) + '\n').encode())

    def get_outputs(self, include_bytecode=True):
        names = checked_resource_files(Path.cwd())
        target = Path(self.build_lib) / 'reproloop' / '_assets'
        return [*super().get_outputs(include_bytecode), str(target / 'manifest.json'),
                *(str(target / name) for name in names)]


setup(cmdclass={'build_py': RuntimeAssetBuild})
