"""``voiced install --json-progress``: one JSON object per line (ADR 0018).

The menu bar app runs ``voiced install --origin app --json-progress`` and
reads these events from its stdout; human output goes to stderr. The first
line is ``hello``, the last ``done`` or ``error``, and the exit code is 0
only with ``done``. Readers ignore unknown events and fields, so adding
either keeps protocol 1.

Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal, TextIO

from voiced import __version__

PROTOCOL = 1
DOWNLOAD_INTERVAL_S = 0.25
"""At most four ``download`` events a second (models download one after another)."""

StepState = Literal["planned", "start", "done"]

_DOWNLOAD = '{"event": "download"'


def download_line(model: str, done: int, total: int) -> str:
    """The line a step's child process prints to report download progress;
    :meth:`Progress.relay` turns it into a ``download`` event."""
    return json.dumps({"event": "download", "model": model, "bytes": done, "total": total})


class Progress:
    """Writes the protocol's events to ``out``. With ``out`` None (a plain
    ``voiced install``) every method does nothing."""

    def __init__(
        self, out: TextIO | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.out = out
        self.clock = clock
        self.step_id: str | None = None
        """The step running now, named by an ``error``."""
        self._last_download: tuple[str, float] | None = None

    def emit(self, event: str, **fields: object) -> None:
        if self.out is None:
            return
        self.out.write(json.dumps({"event": event, **fields}) + "\n")
        self.out.flush()

    def hello(self) -> None:
        self.emit("hello", protocol=PROTOCOL, version=__version__)

    def step(self, step_id: str, state: StepState, shown: str) -> None:
        """A dry run lists each step as ``planned``; a real run sends
        ``start`` and ``done``."""
        self.step_id = step_id if state == "start" else None
        self.emit("step", step=step_id, state=state, shown=shown)

    def download(self, model: str, done: int, total: int) -> None:
        """Throttled to :data:`DOWNLOAD_INTERVAL_S`; the first event of a
        model and its last (``done == total``) always go out."""
        now = self.clock()
        last = self._last_download
        if (
            done < total
            and last is not None
            and last[0] == model
            and now - last[1] < DOWNLOAD_INTERVAL_S
        ):
            return
        self._last_download = (model, now)
        self.emit("download", model=model, bytes=done, total=total)

    def log(self, line: str) -> None:
        self.emit("log", line=line)

    def relay(self, line: str) -> None:
        """One output line of a step's child process: a :func:`download_line`
        becomes a ``download`` event, anything else a ``log`` line."""
        if line.startswith(_DOWNLOAD):
            with contextlib.suppress(ValueError, KeyError, TypeError):
                data = json.loads(line)
                self.download(str(data["model"]), int(data["bytes"]), int(data["total"]))
                return
        self.log(line)

    def done(self, staging_dir: Path) -> None:
        self.emit("done", staging_dir=str(staging_dir))

    def error(self, message: str) -> None:
        """``step`` is None when no step was running."""
        self.emit("error", step=self.step_id, message=message)

    @contextlib.contextmanager
    def reporting(self) -> Iterator[None]:
        """Any exception ends the stream with an ``error`` and goes on."""
        try:
            yield
        except BaseException as exc:
            self.error(str(exc) or type(exc).__name__)
            raise
