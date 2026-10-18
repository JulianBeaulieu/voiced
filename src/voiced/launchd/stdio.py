"""Point a daemon's stdout and stderr at its log files, opened by the daemon.

The plists do not set ``StandardOutPath``/``StandardErrorPath``: launchd
opens those paths as root before it drops to ``UserName``, and they live in
a directory the user owns, so a symlink planted there would have root
create or append to any file. Instead the plist names the files in
``VOICED_STDIO_LOG`` and the process, already running as the user, opens
them itself without following a symlink and duplicates them onto fds 1 and
2, so tracebacks and C-level output (MLX, espeak-ng) still land in them
(ADR 0006).

Standard library only: this runs before anything else is imported.
"""

from __future__ import annotations

import fcntl
import os
import stat
import sys
from pathlib import Path

from voiced.launchd.plist import ENV_STDIO_LOG, log_paths

_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_log(path: Path) -> int:
    """Open ``path`` for appending, creating it ``0600``.

    Refuses a symlink (``O_NOFOLLOW``, ``ELOOP``) and anything that is not a
    regular file; ``O_NONBLOCK`` keeps a FIFO from blocking the open, and is
    cleared again once the file is known to be regular.
    """
    fd = os.open(path, _FLAGS, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


def redirect_stdio(stdout: Path, stderr: Path) -> None:
    """Open both files, then make fds 1 and 2 point at them.

    Both are opened before either is duplicated, so a failure leaves the
    original stdout and stderr untouched.
    """
    stdout.parent.mkdir(parents=True, exist_ok=True)
    out = open_log(stdout)
    try:
        err = open_log(stderr)
    except BaseException:
        os.close(out)
        raise
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()
    os.dup2(out, 1)
    os.dup2(err, 2)
    os.close(out)
    os.close(err)


def redirect_stdio_from_env() -> None:
    """Redirect when ``VOICED_STDIO_LOG`` is set, then unset it.

    Unsetting makes a second call a no-op and keeps it out of child
    processes, which inherit fds 1 and 2 anyway. A file that cannot be
    opened (a symlink, a permission error) is reported on the original
    stderr and the process carries on with it rather than crash-looping.
    """
    prefix = os.environ.pop(ENV_STDIO_LOG, "")
    if not prefix:
        return
    path = Path(prefix)
    paths = log_paths(path.parent, path.name)
    try:
        redirect_stdio(paths.stdout, paths.stderr)
    except OSError as exc:
        print(f"voiced: not redirecting stdout/stderr: {exc}", file=sys.stderr)
