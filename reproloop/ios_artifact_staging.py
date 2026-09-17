"""Materialize the selected inert iOS app for a fixed native consumer.

This bounded file primitive does not admit an execution, release a disk
reservation, sign code, or claim native cleanup. The caller owns the new
directory, including partial output after failure.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from .core import ContractError, require
from .ios_artifact_transfer import (
    MAX_APP_ENTRIES, MAX_CODE_OBJECTS, MAX_EXPANDED_APP_BYTES,
    _app_model, _check_relative, _file_stat_signature, _open_directory_path,
    _open_relative_file, _opened_ipa_contents, _owned_directory, _scan_tree,
    parse_ios_artifact, require_ios_artifact,
)


def _directory_at(root_fd, relative):
    descriptor = os.dup(root_fd)
    try:
        if relative:
            _check_relative(relative)
            for part in relative.split('/'):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=descriptor)
                os.close(descriptor); descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _parent(root_fd, relative):
    path = PurePosixPath(relative)
    return _directory_at(root_fd, '' if str(path.parent) == '.' else str(path.parent)), path.name


def _copy_file(tree, entry, root_fd):
    source = destination = parent = None
    try:
        source = _open_relative_file(tree.root, entry.path, expected_signature=entry.stat_signature)
        parent, name = _parent(root_fd, entry.path)
        destination = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o700 if entry.executable else 0o600, dir_fd=parent)
        checksum = hashlib.sha256(); remaining = entry.size
        while remaining:
            block = os.read(source, min(1024 * 1024, remaining))
            require(bool(block), 'iOS staging source changed')
            remaining -= len(block); checksum.update(block)
            offset = 0
            while offset < len(block):
                count = os.write(destination, block[offset:])
                require(count > 0, 'iOS staging write failed')
                offset += count
        require(not os.read(source, 1) and checksum.hexdigest() == entry.sha256
                and _file_stat_signature(os.fstat(source)) == entry.stat_signature,
                'iOS staging source changed')
        os.fsync(destination); os.fsync(parent)
    finally:
        for descriptor in (destination, source, parent):
            if descriptor is not None:
                os.close(descriptor)


def _copy_tree(tree, output, expected_app_digest):
    parent = root_fd = None
    try:
        parent = _open_directory_path(output.parent)
        parent_info = os.fstat(parent)
        require(parent_info.st_uid == os.getuid() and not parent_info.st_mode & 0o022,
                'iOS staging parent is not owned')
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
        root_fd = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        root_identity = os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino
        for path in sorted(tree.directories, key=lambda item: (item.count('/'), item)):
            directory, name = _parent(root_fd, path)
            try:
                os.mkdir(name, mode=0o700, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
        for entry in tree.files:
            _copy_file(tree, entry, root_fd)
        for link in tree.symlinks:
            directory, name = _parent(root_fd, link.path)
            try:
                os.symlink(link.target, name, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
        os.fsync(root_fd); os.fsync(parent)
        current = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        require(stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == root_identity,
                'iOS staging output changed')
        staged = parse_ios_artifact(output)
        require(staged.app_digest == expected_app_digest, 'iOS staging artifact differs')
        return staged
    finally:
        for descriptor in (root_fd, parent):
            if descriptor is not None:
                os.close(descriptor)


@contextmanager
def _selected_tree(artifact):
    if artifact.format == 'ipa':
        with _opened_ipa_contents(artifact._source, max_bytes=MAX_EXPANDED_APP_BYTES,
                                 max_entries=MAX_APP_ENTRIES) as (root, size, checksum):
            require(size == artifact.container_bytes and checksum == artifact.container_digest,
                    'iOS staging container differs')
            yield _checked_tree(root, artifact)
    else:
        _owned_directory(artifact._source)
        yield _checked_tree(artifact._source, artifact)


def _checked_tree(root, artifact):
    tree = _scan_tree(root, max_bytes=MAX_EXPANDED_APP_BYTES, max_entries=MAX_APP_ENTRIES)
    _, app_digest = _app_model(tree, max_code_objects=MAX_CODE_OBJECTS)
    require(app_digest == artifact.app_digest, 'iOS staging source differs')
    return tree


def stage_ios_artifact(artifact, output_new):
    """Copy exact app contents; an IPA's container identity stays on the input.

    A returned app capability describes the new directory. Its app digest
    equals the selected input, while an IPA's container digest is different.
    Profile bytes stay out of public manifests. Reserve output and temporary
    capacity in the calling operation before using this file primitive.
    """
    require_ios_artifact(artifact)
    try:
        output = Path(output_new)
        require(output.is_absolute() and output.name.casefold().endswith('.app')
                and '..' not in output.parts, 'Invalid iOS staging output')
        require(not output.is_relative_to(artifact._source)
                and not artifact._source.is_relative_to(output), 'iOS staging overlaps the selected source')
        # Refuse parent aliases before any directory is created or bytes copied.
        descriptor = _open_directory_path(output.parent); os.close(descriptor)
        with _selected_tree(artifact) as tree:
            return _copy_tree(tree, output, artifact.app_digest)
    except ContractError:
        raise
    except (OSError, ValueError, TypeError):
        raise ContractError('iOS artifact staging failed') from None


__all__ = ['stage_ios_artifact']
