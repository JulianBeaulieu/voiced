"""``voiced install --json-progress``: one JSON object per line (ADR 0018).

The menu bar app runs ``voiced install --origin app --json-progress`` and
reads these events from its stdout; human output goes to stderr. The first
line is ``hello``, the last ``done`` or ``error``, and the exit code is 0
only with ``done``. Readers ignore unknown events and fields, so adding
either keeps protocol 1.

The model download (``python -m voiced.models download --report``) tells
the installer about itself on lines that start with :data:`DOWNLOAD_PREFIX`,
never on a line that merely looks like an event. Each line's ``kind``
says what it is: ``model`` names the model, revision and cache it fetches,
which a models-step ``error`` then carries, and ``bytes`` its byte counts,
which become ``download`` events.

Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import signal
import threading
from collections.abc import Iterator
from types import FrameType
from typing import Literal, TextIO

from voiced import __version__

PROTOCOL = 1
DOWNLOAD_PREFIX = "@@voiced-download "

ErrorCode = Literal[
    "token_missing", "token_invalid", "gated_terms", "network", "disk", "busy", "in_use", "failed"
]
"""What an ``error`` event's ``code`` says went wrong (ADR 0022 section 4):
a 401 without a token, a token Hugging Face rejected, a gated model's
terms not accepted, no network, not enough disk, an install holding the
state directory, the model the config uses (``delete``), anything else.
The installer's own errors are ``failed``. Readers show an unknown code as
text."""

StepState = Literal["planned", "start", "done"]


class Terminated(SystemExit):
    """SIGTERM (the app's Cancel), raised where the install is, so the
    stream still ends with ``error`` and a step's child is stopped."""

    def __init__(self) -> None:
        super().__init__(128 + signal.SIGTERM)

    def __str__(self) -> str:
        return "terminated (SIGTERM)"


@contextlib.contextmanager
def exit_on_sigterm() -> Iterator[None]:
    def handler(signum: int, frame: FrameType | None) -> None:
        raise Terminated

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class Progress:
    """Writes the protocol's events to ``out``. With ``out`` None (a plain
    ``voiced install``) every method does nothing. Events from several
    threads (a download's byte reports) are written whole, one at a time."""

    def __init__(self, out: TextIO | None = None) -> None:
        self.out = out
        # Reentrant: a signal handler that ends the stream runs in the main
        # thread, which may be inside emit() already.
        self._lock = threading.RLock()
        self.step_id: str | None = None
        """The step running now, named by an ``error``."""
        self.fetching: dict[str, object] = {}
        """``repo``, ``revision`` and ``path`` of the model the download is on."""

    @property
    def streaming(self) -> bool:
        return self.out is not None

    def emit(self, event: str, **fields: object) -> None:
        with self._lock:
            if self.out is None:
                return
            self.out.write(json.dumps({"event": event, **fields}) + "\n")
            self.out.flush()

    def close_with_error(self, message: str, code: ErrorCode = "failed") -> None:
        """The last event: ``error``, after which nothing more is written, so
        a byte report from another thread cannot follow it."""
        with self._lock:
            self.error(message, code)
            self.out = None

    def hello(self) -> None:
        self.emit("hello", protocol=PROTOCOL, version=__version__)

    def step(self, step_id: str, state: StepState, shown: str) -> None:
        """A dry run lists each step as ``planned``; a real run sends
        ``start`` and ``done``."""
        self.step_id = step_id if state == "start" else None
        self.emit("step", step=step_id, state=state, shown=shown)

    def log(self, line: str) -> None:
        """One output line of a step's child process."""
        self.emit("log", line=line)

    def child_line(self, line: str) -> bool:
        """Take a download child's own line (:data:`DOWNLOAD_PREFIX`); False
        for any other line, which is output to log."""
        if not line.startswith(DOWNLOAD_PREFIX):
            return False
        try:
            fields = json.loads(line.removeprefix(DOWNLOAD_PREFIX))
        except ValueError:
            return False
        if not isinstance(fields, dict) or not isinstance(fields.get("repo"), str):
            return False
        if fields.get("kind") == "model":
            self.fetching = {k: fields.get(k) for k in ("repo", "revision", "path")}
            return True
        if fields.get("kind") != "bytes":
            return False
        counts = (fields.get("bytes"), fields.get("total"))
        if not all(isinstance(n, int) and not isinstance(n, bool) for n in counts):
            return False
        self.emit("download", model=fields["repo"], bytes=counts[0], total=counts[1])
        return True

    def done(self) -> None:
        """The last event on success; like :meth:`close_with_error`, nothing
        is written after it, so a SIGTERM now cannot add an ``error``."""
        with self._lock:
            self.emit("done")
            self.out = None

    def error(self, message: str, code: ErrorCode = "failed") -> None:
        """``step`` is None when no step was running. A models-step error
        names the model it was on, for the app's manual fallback and its
        Show in Finder. ``code`` says what went wrong for the app to act on
        (``voiced models download``'s, ADR 0022); the installer's is
        ``failed``. A model download's error names its model too."""
        extra = self.fetching if self.step_id in ("models", None) else {}
        self.emit("error", step=self.step_id, code=code, message=message, **extra)

    def fail_before_start(self, message: str) -> None:
        """For a failure before the install could start (a config file that
        does not parse): the stream still opens and ends."""
        self.hello()
        self.error(message)

    @contextlib.contextmanager
    def reporting(self) -> Iterator[None]:
        """Any exception ends the stream with an ``error`` and goes on."""
        try:
            yield
        except BaseException as exc:
            self.error(str(exc) or type(exc).__name__)
            raise
