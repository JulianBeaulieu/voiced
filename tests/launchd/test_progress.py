"""``voiced install --json-progress``: the event stream the app reads (ADR 0018)."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from voiced import __version__, cli_lifecycle
from voiced.cli import build_parser, main
from voiced.config import Settings
from voiced.launchd.control import CommandResult
from voiced.launchd.progress import Progress, download_line

from .test_install import _compare, _environment

MODEL = "mlx-community/parakeet-tdt-0.6b-v3"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _events(text: str) -> list[dict[str, Any]]:
    """Every stdout line is one JSON object; nothing else is on stdout."""
    return [json.loads(line) for line in text.splitlines()]


def test_download_is_throttled_but_keeps_first_and_last() -> None:
    out, clock = io.StringIO(), Clock()
    progress = Progress(out, clock=clock)
    progress.download(MODEL, 0, 100)  # the first of a model
    clock.now += 0.1
    progress.download(MODEL, 10, 100)  # too soon
    progress.download("other/model", 5, 50)  # another model's first
    clock.now += 0.1
    progress.download(MODEL, 20, 100)  # the last event was another model's
    clock.now += 0.06
    progress.download(MODEL, 30, 100)  # too soon again
    progress.download(MODEL, 100, 100)  # the last, however soon
    assert [(e["model"], e["bytes"]) for e in _events(out.getvalue())] == [
        (MODEL, 0),
        ("other/model", 5),
        (MODEL, 20),
        (MODEL, 100),
    ]


def test_at_most_four_downloads_a_second() -> None:
    out, clock = io.StringIO(), Clock()
    progress = Progress(out, clock=clock)
    for n in range(100):  # 100 updates over one second
        progress.download(MODEL, n, 1000)
        clock.now += 0.01
    assert len(_events(out.getvalue())) == 4


def test_relay_turns_a_download_line_into_an_event() -> None:
    out = io.StringIO()
    progress = Progress(out)
    progress.relay(download_line(MODEL, 5, 10))
    progress.relay('{"event": "download", "model": 1}')  # malformed: just a line
    progress.relay("Fetching 3 files")
    assert _events(out.getvalue()) == [
        {"event": "download", "model": MODEL, "bytes": 5, "total": 10},
        {"event": "log", "line": '{"event": "download", "model": 1}'},
        {"event": "log", "line": "Fetching 3 files"},
    ]


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
    return text.replace(str(tmp_path), "<tmp>").replace(json.dumps(__version__), '"<version>"')


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
    state, stage = str(tmp_path / "state"), str(tmp_path / "stage")
    return ["install", "--json-progress", "--state-dir", state, "--staging-dir", stage, *extra]


@pytest.mark.usefixtures("running_env")
def test_dry_run_lists_the_steps(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(tmp_path, "--dry-run")) == 0
    out, err = capsys.readouterr()
    _compare(_normalise(out, tmp_path), "install-dry-run.jsonl")
    assert "(would be written)" in err and "<plist" in err  # the human output
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
        on_line(download_line(MODEL, 0, 2508))
        on_line(download_line(MODEL, 2508, 2508))
        return CommandResult(0)

    assert run(models) == 0
    out, err = capsys.readouterr()
    _compare(_normalise(out, tmp_path), "install-run.jsonl")
    assert "stt: engine=fake model=m" in err  # the step's output is on stderr too
    assert "sudo bash" in err


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
        "message": "model download failed (exit 1):",
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
