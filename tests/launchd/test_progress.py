"""``voiced install --json-progress``: the event stream the app reads (ADR 0018)."""

from __future__ import annotations

import errno
import io
import json
import os
import re
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from voiced import __version__, cli_lifecycle
from voiced.cli import build_parser, main
from voiced.config import Settings
from voiced.launchd.control import CommandResult
from voiced.launchd.progress import DOWNLOAD_PREFIX, Progress

from .test_install import _compare, _environment


def _events(text: str) -> list[dict[str, Any]]:
    """Every stdout line is one JSON object; nothing else is on stdout."""
    return [json.loads(line) for line in text.splitlines()]


def test_without_a_stream_nothing_is_written(capsys: pytest.CaptureFixture[str]) -> None:
    progress = Progress()
    progress.hello()
    progress.step("config", "start", "x")
    progress.error("boom")
    assert capsys.readouterr() == ("", "")


def test_reporting_names_the_running_step_and_reraises() -> None:
    out = io.StringIO()
    progress = Progress(out)
    with pytest.raises(KeyboardInterrupt), progress.reporting():
        progress.step("models", "start", "download")
        raise KeyboardInterrupt
    assert _events(out.getvalue())[-1] == {
        "event": "error",
        "step": "models",
        "message": "KeyboardInterrupt",
    }


# The whole command -------------------------------------------------------------


def _normalise(text: str, tmp_path: Path) -> str:
    text = text.replace(str(tmp_path), "<tmp>").replace(json.dumps(__version__), '"<version>"')
    return re.sub(r"ports [0-9, ]+ are free", "ports <ports> are free", text)  # free_port()s


@pytest.fixture
def running_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`voiced install` links the environment it runs from: one in tmp_path,
    whose engine check passes. Main reads the log dir from the environment."""
    env = _environment(tmp_path / "env")
    (env / "bin/python").write_text("#!/bin/sh\necho voiced 0.0.1\n")
    monkeypatch.setattr(sys, "prefix", str(env))
    monkeypatch.setenv("VOICED_SERVER__LOG_DIR", str(tmp_path / "logs"))
    return env


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["install", "--json-progress", "--state-dir", str(tmp_path / "state"), *extra]


@pytest.mark.usefixtures("running_env")
def test_dry_run_lists_the_steps(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(tmp_path, "--dry-run")) == 0
    out, err = capsys.readouterr()
    _compare(_normalise(out, tmp_path), "install-dry-run.jsonl")
    assert "No root step" in err  # the human output: a scratch state dir
    assert not (tmp_path / "state").exists()


StreamingFake = Callable[..., CommandResult]


@pytest.fixture
def run(settings: Settings, tmp_path: Path, running_env: Path) -> Callable[[StreamingFake], int]:
    """``voiced install --json-progress`` with a fake model download."""
    return lambda models: _run(settings, tmp_path, models)


def _run(settings: Settings, tmp_path: Path, models: StreamingFake) -> int:
    def runner(argv: Sequence[str], on_line: Callable[[str], None] | None = None) -> CommandResult:
        assert on_line is not None, "--json-progress keeps the steps' output off stdout"
        return models(argv, on_line)

    args = build_parser().parse_args(_argv(tmp_path))
    return cli_lifecycle.cmd_install(args, settings, print, runner=runner)


def test_a_run_reports_every_step_and_ends_with_done(
    run: Callable[[StreamingFake], int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def models(argv: Sequence[str], on_line: Callable[[str], None]) -> CommandResult:
        on_line("stt: engine=fake model=m")
        on_line("  loaded fake (m)")
        return CommandResult(0)

    assert run(models) == 0
    out, err = capsys.readouterr()
    _compare(_normalise(out, tmp_path), "install-run.jsonl")
    assert "stt: engine=fake model=m" in err  # the step's output is on stderr too
    assert "Run `voiced serve` yourself" in err


def test_a_failed_step_ends_with_its_error(
    run: Callable[[StreamingFake], int], capsys: pytest.CaptureFixture[str]
) -> None:
    def models(argv: Sequence[str], on_line: Callable[[str], None]) -> CommandResult:
        return CommandResult(1)

    assert run(models) == 1
    events = _events(capsys.readouterr().out)
    assert events[0]["event"] == "hello"
    assert (events[-2]["step"], events[-2]["state"]) == ("models", "start")
    assert events[-1] == {
        "event": "error",
        "step": "models",
        "message": "model download failed (exit 1)",
    }


def test_an_unexpected_exception_still_ends_with_an_error(
    run: Callable[[StreamingFake], int], capsys: pytest.CaptureFixture[str]
) -> None:
    def models(argv: Sequence[str], on_line: Callable[[str], None]) -> CommandResult:
        raise OSError("No space left on device")

    with pytest.raises(OSError):
        run(models)
    last = _events(capsys.readouterr().out)[-1]
    assert last == {"event": "error", "step": "models", "message": "No space left on device"}


@pytest.mark.usefixtures("running_env")
def test_a_refusal_before_any_step(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/precious.txt").write_text("mine")
    assert main(_argv(tmp_path)) == 1
    events = _events(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["hello", "error"]
    assert events[1]["step"] is None and "precious.txt" in events[1]["message"]

    monkeypatch.setattr(cli_lifecycle, "is_root", lambda: True)
    assert main(_argv(tmp_path)) == 2
    events = _events(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["hello", "error"]
    assert "not with sudo" in events[1]["message"]


def test_a_config_error_still_opens_and_ends_the_stream(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[server\n")
    monkeypatch.setenv("VOICED_CONFIG", str(config))
    assert main(_argv(tmp_path)) == 2
    out, err = capsys.readouterr()
    events = _events(out)
    assert [e["event"] for e in events] == ["hello", "error"]
    assert events[1]["message"].startswith(f"config error at {config}")
    assert "config error" in err


def test_sigterm_ends_with_an_error_and_exit_143(
    run: Callable[[StreamingFake], int], capsys: pytest.CaptureFixture[str]
) -> None:
    """The app's Cancel: the stream still ends with error, and the exit code
    says the install was terminated."""

    def models(argv: Sequence[str], on_line: Callable[[str], None]) -> CommandResult:
        os.kill(os.getpid(), signal.SIGTERM)
        pytest.fail("SIGTERM did not stop the install")

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as info:
        run(models)
    assert info.value.code == 143
    last = _events(capsys.readouterr().out)[-1]
    assert last == {"event": "error", "step": "models", "message": "terminated (SIGTERM)"}
    assert signal.getsignal(signal.SIGTERM) == before  # restored after the install


@pytest.mark.usefixtures("running_env")
def test_sigterm_right_after_hello_still_ends_with_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No gap between the first line and the handler: the stream that has
    begun always gets its last line."""

    def terminated_now() -> bool:
        os.kill(os.getpid(), signal.SIGTERM)
        return False

    monkeypatch.setattr(cli_lifecycle, "is_root", terminated_now)
    with pytest.raises(SystemExit) as info:
        main(_argv(tmp_path))
    assert info.value.code == 143
    events = _events(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["hello", "error"]
    assert events[1] == {"event": "error", "step": None, "message": "terminated (SIGTERM)"}


# The model download's own lines ---------------------------------------------


def _child(**fields: object) -> str:
    return DOWNLOAD_PREFIX + json.dumps(fields)


def _model(**fields: object) -> str:
    return _child(kind="model", **fields)


def _bytes(**fields: object) -> str:
    return _child(kind="bytes", **fields)


def test_the_downloads_byte_counts_become_download_events(
    run: Callable[[StreamingFake], int], capsys: pytest.CaptureFixture[str]
) -> None:
    """Only lines with the child's prefix count; they are not logged, and a
    line that merely looks like an event is output like any other."""

    def models(argv: Sequence[str], on_line: Callable[[str], None]) -> CommandResult:
        assert argv[-1] == "--report"
        on_line(_model(repo="org/m", revision="abc", path="/hf/hub"))
        on_line(_bytes(repo="org/m", bytes=10, total=100))
        on_line(_bytes(repo="org/m", bytes=100, total=100))
        on_line('{"event": "download", "model": "org/m", "bytes": 1, "total": 1}')
        return CommandResult(0)

    assert run(models) == 0
    out, err = capsys.readouterr()
    events = _events(out)
    assert [e for e in events if e["event"] == "download"] == [
        {"event": "download", "model": "org/m", "bytes": 10, "total": 100},
        {"event": "download", "model": "org/m", "bytes": 100, "total": 100},
    ]
    logged = [e["line"] for e in events if e["event"] == "log"]
    assert logged == ['{"event": "download", "model": "org/m", "bytes": 1, "total": 1}']
    assert DOWNLOAD_PREFIX not in err


def test_a_failed_download_names_the_model_and_its_cache(
    run: Callable[[StreamingFake], int], capsys: pytest.CaptureFixture[str]
) -> None:
    """For the app's manual fallback and Show in Finder (ADR 0018)."""

    def models(argv: Sequence[str], on_line: Callable[[str], None]) -> CommandResult:
        on_line(_model(repo="org/a", revision="1" * 40, path="/hf/hub"))
        on_line(_model(repo="org/b", revision=None, path="/hf/hub"))
        return CommandResult(1)

    assert run(models) == 1
    assert _events(capsys.readouterr().out)[-1] == {
        "event": "error",
        "step": "models",
        "message": "model download failed (exit 1)",
        "repo": "org/b",
        "revision": None,
        "path": "/hf/hub",
    }


def test_a_models_error_before_any_model_names_none(
    run: Callable[[StreamingFake], int], capsys: pytest.CaptureFixture[str]
) -> None:
    """The download can fail before it says which model it is on (a config
    it cannot read, an engine that does not import)."""

    def models(argv: Sequence[str], on_line: Callable[[str], None]) -> CommandResult:
        on_line("Traceback (most recent call last):")
        return CommandResult(1)

    assert run(models) == 1
    assert _events(capsys.readouterr().out)[-1] == {
        "event": "error",
        "step": "models",
        "message": "model download failed (exit 1)",
    }


def test_only_a_models_error_names_the_model() -> None:
    out = io.StringIO()
    progress = Progress(out)
    progress.step("models", "start", "download")
    assert progress.child_line(_model(repo="org/a", revision="r", path="/hf"))
    progress.step("models", "done", "download")
    progress.step("link", "start", "link")
    progress.error("boom")
    assert _events(out.getvalue())[-1] == {"event": "error", "step": "link", "message": "boom"}


@pytest.mark.parametrize(
    "line",
    [
        "plain output",
        DOWNLOAD_PREFIX + "not json",
        DOWNLOAD_PREFIX + "[1, 2]",
        _bytes(bytes=1, total=2),  # no repo
        _bytes(repo="org/m", bytes="1", total=2),
        _bytes(repo="org/m", bytes=True, total=2),
        _bytes(repo="org/m", bytes=1),
        _child(repo="org/m", bytes=1, total=2),  # no kind
        _child(kind="other", repo="org/m", revision="r", path="/hf"),
    ],
)
def test_lines_that_are_not_the_childs_are_output(line: str) -> None:
    out = io.StringIO()
    assert not Progress(out).child_line(line)
    assert out.getvalue() == ""


def test_a_port_in_use_fails_before_the_models(
    run: Callable[[StreamingFake], int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from voiced.launchd import ports

    in_use = OSError(errno.EADDRINUSE, "Address already in use")
    monkeypatch.setattr(ports, "bind_error", lambda bind, port: in_use)
    monkeypatch.setattr(ports, "listener_pids", lambda port, runner: [4242])
    monkeypatch.setattr(ports, "process_of", lambda pid: ("nc", ["nc", "-l"]))

    def models(argv: Sequence[str], on_line: Callable[[str], None]) -> CommandResult:
        pytest.fail("the models step ran")

    assert run(models) == 1
    last = _events(capsys.readouterr().out)[-1]
    assert last["step"] == "ports"
    assert "is in use by nc (pid 4242)" in last["message"]
