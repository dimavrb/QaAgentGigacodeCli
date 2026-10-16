"""Local POSIX file operations: no symlink traversal, durable publication, directory locks."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import stat
import time


@contextmanager
def directory(path, *, create=False):
    path = Path(path).absolute()
    descriptors = [os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for name in path.parts[1:]:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptors[-1])
                    os.fsync(descriptors[-1])
                except FileExistsError:
                    pass
            descriptors.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_bytes(path, *, maximum=8 * 1024 * 1024):
    path = Path(path)
    with directory(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('file must be a regular file')
            if info.st_size > maximum:
                raise ValueError('local configuration exceeds the size limit')
            data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError('local configuration exceeds the size limit')
            return data


def atomic_bytes(path, data, *, replace=True):
    path = Path(path)
    with directory(path.parent, create=True) as parent:
        temporary = '.qa-' + secrets.token_hex(12)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                try:
                    os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    return False
            os.fsync(parent)
            return True
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.fsync(parent)


def atomic_text(path, text, *, replace=True):
    return atomic_bytes(path, text.encode('utf-8'), replace=replace)


@contextmanager
def project_lock(project, *, timeout=10):
    # Lock the existing directory inode: init still publishes exactly three files.
    with directory(project) as descriptor:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('project configuration is busy; retry after the other writer finishes')
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
