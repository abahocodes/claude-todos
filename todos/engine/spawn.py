"""Start the background worker if one isn't already running. Never blocks the caller."""
import os
import subprocess
import sys

from ..core import paths
from ..core.lock import locked

WORKER_LOCK = ".worker.lock"
RECURSION_ENV = "TODOS_WORKER"


def worker_running() -> bool:
    with locked(WORKER_LOCK, blocking=False) as got:
        return not got


def ensure_worker() -> bool:
    """Returns True if a new worker was started."""
    if worker_running():
        return False
    paths.ensure_dirs()
    env = dict(os.environ, **{RECURSION_ENV: "1"})
    log = open(paths.path("logs", "worker.log"), "a")
    subprocess.Popen(
        [sys.executable, str(paths.TODO_BIN), "reconcile", "--worker"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, env=env, cwd=str(paths.home()),
    )
    return True
