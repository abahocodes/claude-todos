"""One lock for every write to the data folder, plus atomic file replacement."""
import contextlib
import fcntl
import os
import time
from pathlib import Path

from . import paths


class LockTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def locked(name: str = ".lock", timeout: float = 15.0, blocking: bool = True):
    """Exclusive flock on <home>/<name>. Non-blocking mode yields False if busy."""
    paths.ensure_dirs()
    fd = os.open(paths.path(name), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if not blocking:
                    break
                if time.monotonic() > deadline:
                    raise LockTimeout(f"could not lock {name} within {timeout}s")
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
