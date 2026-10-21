"""Speech-to-text encoder calls go first on the GPU; synthesis waits (ADR 0019).

The STT and TTS services are separate processes on one GPU. Their command
buffers take turns (as measured, not documented), and a streaming encoder
call is many short GPU round trips, so each one waited behind a whole buffer
of Kokoro's work: a push-to-talk finalize took 1.4 times as long in the
median (1.6 times at worst) while Kokoro synthesized.

The two services agree through an advisory lock on one file per user
(:func:`lock_path`), not through a socket or a server: the STT holds it
exclusively for each encoder call a client waits on (:func:`stt_turn`): the
call at a pause, the calls after the end of the stream, and every call of a
session that is behind. The TTS engine, before it submits each piece of a
sentence to the GPU, waits while the lock is held (:class:`Yielder`), up to
:data:`MAX_WAIT_S` per sentence. The kernel
drops the lock when its holder exits, so a crashed or killed STT never leaves
synthesis waiting, and a TTS that never looks costs the STT nothing.

Locks belong to an open file, not to a process, so the holder and the probe
each open the file themselves: in a single process (the end-to-end tests run
both services in one) they still exclude each other. Every failure to open or
lock the file, or finding it held by another process, is logged once and
treated as "no STT call running": the gate is an optimisation and must
never stop either service.

Standard library only.
"""

from __future__ import annotations

import fcntl
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "LOCK_NAME",
    "MAX_WAIT_S",
    "TAKE_S",
    "Turn",
    "Yielder",
    "lock_path",
    "stt_running",
    "stt_turn",
]

LOCK_NAME = "voiced-gpu.lock"
MAX_WAIT_S = 0.3
"""Most a sentence waits for STT encoder calls in all. One call takes 100 to
200 ms, so this covers the call running when the sentence starts and most of
the next; a session catching up on a backlog runs calls back to back and must
not stop synthesis for seconds."""
POLL_S = 0.001
TAKE_S = 0.005
"""Most the STT tries to take the lock before running a call without it. A
TTS probe holds it shared for microseconds; anything longer is another
process holding it (an old instance during a restart, a second copy started
by hand), and the STT must never wait for that."""
_CS_DARWIN_USER_TEMP_DIR = 65537
"""``confstr`` name for the per-user temporary directory under ``/var/folders``."""

_lock = threading.Lock()
_fds: dict[str, int] = {}
_warned = False


def lock_path() -> Path:
    """``voiced-gpu.lock`` in the user's temporary directory.

    The directory comes from ``confstr``, not ``$TMPDIR``: launchd jobs start
    without ``TMPDIR``, and a service started from a terminal must find the
    same file as one launchd started.
    """
    base = ""
    if sys.platform == "darwin":
        try:
            base = os.confstr(_CS_DARWIN_USER_TEMP_DIR) or ""
        except (ValueError, OSError):
            base = ""
    return Path(base or tempfile.gettempdir()) / LOCK_NAME


def _fd(role: str) -> int | None:
    """This process's descriptor for ``role`` ("holder" or "probe"), opened
    on first use and shared by every thread of the process (the STT takes the
    holder on its one model thread only); ``None`` when the file cannot be
    opened or belongs to another user (the ``tempfile`` fallback directory is
    shared)."""
    with _lock:
        fd = _fds.get(role)
        if fd is None:
            path = lock_path()
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            except OSError as exc:
                _warn(f"cannot open {path}: {exc}")
                return None
            if os.fstat(fd).st_uid != os.getuid():
                os.close(fd)
                _warn(f"{path} belongs to another user")
                return None
            _fds[role] = fd
        return fd


def _warn(message: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.warning("GPU priority for speech-to-text is unavailable: %s", message)


Turn = Callable[[], AbstractContextManager[object]]
"""What an engine holds around an STT call: :func:`stt_turn`, or
``contextlib.nullcontext`` where no client is waiting."""


@contextmanager
def stt_turn() -> Iterator[None]:
    """Hold the GPU lock for one STT encoder call, if it can be had at once.

    It is tried for at most :data:`TAKE_S` without blocking; if another
    process holds it, the call runs without it.
    """
    fd = _fd("holder")
    held = False
    if fd is not None:
        deadline = time.perf_counter() + TAKE_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.perf_counter() >= deadline:
                    _warn(f"{lock_path()} is held by another process")
                    break
                time.sleep(POLL_S / 4)
            except OSError as exc:
                _warn(f"cannot lock {lock_path()}: {exc}")
                break
    try:
        yield
    finally:
        if held and fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)


def stt_running() -> bool:
    """Whether an STT encoder call holds the lock now. Never waits."""
    fd = _fd("probe")
    if fd is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError as exc:
        _warn(f"cannot lock {lock_path()}: {exc}")
        return False
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


class Yielder:
    """Waits for STT encoder calls before each piece of one sentence's GPU
    work, within a budget for the whole sentence. One per sentence."""

    def __init__(self, budget_s: float = MAX_WAIT_S) -> None:
        self._budget_s = budget_s
        self.waited_s = 0.0

    def __call__(self) -> None:
        if self.waited_s >= self._budget_s or not stt_running():
            return
        started = time.perf_counter()
        deadline = started + self._budget_s - self.waited_s
        while time.perf_counter() < deadline and stt_running():
            time.sleep(POLL_S)
        self.waited_s += time.perf_counter() - started
