"""Point a daemon's stdout and stderr at its log files, opened by the daemon.

The plists do not set ``StandardOutPath``/``StandardErrorPath``: launchd
may open those paths as root before it drops to ``UserName``, and they live
in a directory the user owns, so a symlink planted there could have root
create or append to any file. Instead the plist names the files in
``VOICED_STDIO_LOG`` and the process, already running as the user, opens
them itself without following a symlink and duplicates them onto fds 1 and
2, so tracebacks and C-level output (MLX, espeak-ng) still land in them
(ADR 0006). The plists always name the default log directory; a custom
``[server] log_dir`` takes over once the config is read
(:func:`follow_log_dir`).

Standard library only: this runs before anything else is imported.
"""

from __future__ import annotations

import fcntl
import logging
import os
import stat
import sys
from pathlib import Path

from voiced.launchd.plist import ENV_STDIO_LOG, log_paths

_redirect_error: OSError | None = None
"""Why the last redirect failed, until :func:`log_redirect_error` logs it."""
_prefix: Path | None = None
"""The ``<log_dir>/<service>`` last redirected to (or tried)."""

_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_log(path: Path) -> int:
    """Open ``path`` for appending, creating it ``0600`` (and tightening a
    file an older plist had launchd create).

    Refuses a symlink (``O_NOFOLLOW``, ``ELOOP``) and anything that is not a
    regular file; ``O_NONBLOCK`` keeps a FIFO from blocking the open, and is
    cleared again once the file is known to be regular.
    """
    fd = os.open(path, _FLAGS, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        os.fchmod(fd, 0o600)
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


def redirect_stdio(stdout: Path, stderr: Path) -> None:
    """Open both files, then make fds 1 and 2 point at them.

    Both are opened before either is duplicated, so a failure leaves the
    original stdout and stderr untouched. If fd 1 or 2 was closed at startup
    the open reuses it; that descriptor is then the target and is kept.
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
    for fd in (out, err):
        if fd not in (1, 2):
            os.close(fd)


def redirect_stdio_from_env() -> None:
    """Redirect when ``VOICED_STDIO_LOG`` is set, then unset it.

    Unsetting makes a second call a no-op and keeps it out of child
    processes, which inherit fds 1 and 2 anyway. A file that cannot be
    opened (a symlink, a permission error) is reported on the original
    stderr, which under launchd is ``/dev/null``, and kept for
    :func:`log_redirect_error` to put in the application log once that is
    configured. The process carries on rather than crash-looping.
    """
    prefix = os.environ.pop(ENV_STDIO_LOG, "")
    if prefix:
        _redirect(Path(prefix))


def follow_log_dir(log_dir: Path) -> None:
    """Redirect again into ``log_dir`` if it is not where the plist pointed.

    The plists name the default log directory, since root renders them from
    the user name alone (ADR 0018); the config's ``[server] log_dir`` wins
    once it is read. The first redirect still happens before, so a config
    error lands in the default log. A no-op when nothing was redirected.
    """
    if _prefix is not None and _prefix.parent != log_dir:
        _redirect(log_dir / _prefix.name)


def _redirect(prefix: Path) -> None:
    global _redirect_error, _prefix
    _prefix = prefix
    paths = log_paths(prefix.parent, prefix.name)
    try:
        redirect_stdio(paths.stdout, paths.stderr)
    except OSError as exc:
        _redirect_error = exc
        print(f"voiced: not redirecting stdout/stderr: {exc}", file=sys.stderr)


def log_redirect_error(logger: logging.Logger | None = None) -> None:
    """Log a failed redirect once as a warning; called by the log configurers."""
    global _redirect_error
    exc, _redirect_error = _redirect_error, None
    if exc is not None:
        (logger or logging.getLogger(__name__)).warning(
            "stdout and stderr are not redirected to their log files: %s", exc
        )
