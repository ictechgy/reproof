"""Capped local-only birth output. Never an export, upload or source input."""
import os
import re
import stat

from reproof.core import ContractError
from .android_recovery_authority import _directory

_MAXIMUM = 65537
_DIRECTORY = 'credentials'


def _require(value):
    if not value:
        raise ContractError('Private diagnostic evidence boundary rejected')


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _EvidenceSink:
    """One collector owns writes/finish; the parent may finish only after join."""
    def __init__(self, root, root_fd, directory_fd, name):
        _require(type(name) is str and re.fullmatch(r'[0-3]-(stdout|stderr)\.bin', name))
        self.root, self.root_fd, self.directory_fd, self.name = root, root_fd, directory_fd, name
        self.fd = None
        self.bytes_written = 0
        self.failed = False
        self.finished = False
        self._check_directory()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=directory_fd)
        try:
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid())
            self._stamp = _stamp(info)
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def _check_directory(self):
        current = _directory(self.root)
        try:
            before, after = os.fstat(self.root_fd), os.fstat(current)
            _require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino))
        finally:
            os.close(current)
        directory = os.fstat(self.directory_fd)
        rebound = os.stat(_DIRECTORY, dir_fd=self.root_fd, follow_symlinks=False)
        _require(stat.S_ISDIR(directory.st_mode) and stat.S_IMODE(directory.st_mode) == 0o700
                 and directory.st_uid == os.getuid()
                 and (directory.st_dev, directory.st_ino) == (rebound.st_dev, rebound.st_ino))

    def _check(self):
        self._check_directory()
        info = os.fstat(self.fd)
        rebound = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
        _require(_stamp(info) == self._stamp and _stamp(rebound) == self._stamp
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                 and info.st_size == self.bytes_written)

    def append(self, chunk):
        if self.failed or self.finished:
            return False
        try:
            self._check()
            prefix = memoryview(chunk)[:max(0, _MAXIMUM-self.bytes_written)]
            while prefix:
                count = os.write(self.fd, prefix)
                _require(0 < count <= len(prefix))
                self.bytes_written += count
                prefix = prefix[count:]
            self._stamp = _stamp(os.fstat(self.fd))
            self._check()
            return True
        except (OSError, ValueError, ContractError):
            self.failed = True
            return False

    def finish(self):
        if self.finished:
            return not self.failed
        try:
            if not self.failed:
                self._check()
                os.fsync(self.fd)
                os.fsync(self.directory_fd)
        except (OSError, ValueError, ContractError):
            self.failed = True
        finally:
            fd, self.fd = self.fd, None  # Never retry a possibly consumed FD.
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    self.failed = True
        self.finished = True
        return not self.failed

    def summary(self):
        return {'localOnly': True, 'prefixOnly': True, 'maxBytes': _MAXIMUM, 'bytesWritten': self.bytes_written,
                'complete': self.finished and not self.failed,
                'incomplete': self.failed}
