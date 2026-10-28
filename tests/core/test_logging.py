"""Which handlers ``configure_logging`` installs, in a terminal and under launchd."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from voiced.launchd.plist import OLD_PLIST_ENV, log_paths
from voiced.logging import BACKUP_COUNT, MAX_BYTES, configure_logging
from voiced.watchdog import configure_watchdog_logging


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def _stderr_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if type(h) is logging.StreamHandler and h.stream is sys.stderr
    ]


def _file_handlers() -> list[logging.handlers.RotatingFileHandler]:
    return [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]


def test_daemon_logs_to_rotating_file_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under launchd stderr is an unrotated file; nothing is logged there."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    configure_logging("tts", "info", tmp_path)
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert _stderr_handlers() == []
    [file_handler] = _file_handlers()
    assert root.handlers == [file_handler]
    assert file_handler.level == logging.NOTSET  # inherits the root level
    assert Path(file_handler.baseFilename) == tmp_path / "tts.log"
    assert (file_handler.maxBytes, file_handler.backupCount) == (MAX_BYTES, BACKUP_COUNT)

    logging.getLogger("voiced.tts").info("hello")
    file_handler.flush()
    assert (tmp_path / "tts.log").read_text().count("hello") == 1


def test_terminal_with_log_dir_logs_to_both(tmp_path: Path) -> None:
    configure_logging("stt", "debug", tmp_path, interactive=True)
    assert len(_stderr_handlers()) == 1
    assert len(_file_handlers()) == 1
    assert len(logging.getLogger().handlers) == 2
    assert logging.getLogger().level == logging.DEBUG


def test_without_log_dir_logs_to_stderr_only() -> None:
    configure_logging("stt", "warning", None, interactive=False)
    [stream] = _stderr_handlers()
    assert logging.getLogger().handlers == [stream]
    assert _file_handlers() == []
    assert logging.getLogger().level == logging.WARNING


def test_interactive_defaults_to_whether_stderr_is_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    configure_logging("tts", "info", tmp_path)
    assert _stderr_handlers() == []
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    configure_logging("tts", "info", tmp_path)
    assert len(_stderr_handlers()) == 1


def test_reconfiguring_replaces_handlers(tmp_path: Path) -> None:
    configure_logging("tts", "info", tmp_path, interactive=True)
    configure_logging("tts", "info", tmp_path, interactive=True)
    assert len(logging.getLogger().handlers) == 2


@pytest.mark.parametrize("service", ["stt", "watchdog"])
def test_a_plist_of_an_earlier_release_is_logged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    """A formula or uv tool upgrade runs the new code under the old plist,
    which sends stdout and stderr nowhere: the rotating log says so once."""
    monkeypatch.setenv(OLD_PLIST_ENV, str(tmp_path / service))
    for _ in range(2):  # reconfiguring does not repeat it
        if service == "watchdog":
            configure_watchdog_logging("info", log_paths(tmp_path, service).app)
        else:
            configure_logging(service, "info", tmp_path, interactive=False)
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_paths(tmp_path, service).app.read_text()
    assert text.count("plist of an earlier release") == 1 and "WARNING" in text
    assert "`voiced install`" in text
    assert OLD_PLIST_ENV not in os.environ  # nor in what the service starts
