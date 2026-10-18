from __future__ import annotations

import ctypes
import json
import os
import resource
import socket
import subprocess
import sys

import pytest
import uvicorn

from voiced import cli
from voiced.cli import main
from voiced.config import Settings

PRIO_DARWIN_ROLE_UI_FOCAL = 1
"""What ``PRIO_DARWIN_ROLE`` reads for ``TASK_FOREGROUND_APPLICATION``
(XNU ``proc_task_role_to_darwin_role``)."""


def test_config_prints_effective_settings(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", "/nonexistent/voiced.toml")
    monkeypatch.setenv("VOICED_SERVER__BIND", "0.0.0.0")
    assert main(["config"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["server"]["bind"] == "0.0.0.0"
    assert payload["_config_file"] == "/nonexistent/voiced.toml"


@pytest.mark.parametrize(
    ("soft", "hard", "expected", "logged"),
    [
        (256, resource.RLIM_INFINITY, 4096, "raised from 256 to 4096"),  # launchd's default
        (256, 1024, 1024, "raised from 256 to 1024, the hard limit"),
        (256, 256, None, "stays at 256: the hard limit is 256"),
        (10_000, resource.RLIM_INFINITY, None, ""),  # never lowered
    ],
)
def test_open_file_limit_is_raised(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    soft: int,
    hard: int,
    expected: int | None,
    logged: str,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (soft, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append(limits))
    with caplog.at_level("INFO", logger="voiced.cli"):
        cli.raise_open_file_limit()
    assert calls == ([] if expected is None else [(expected, hard)])
    assert logged in caplog.text


def test_serve_raises_the_limit_and_task_role_before_serving(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The service process raises its own limit and sets its task role once,
    before uvicorn accepts a connection; the app (and so every test that runs
    a lifespan) does not."""
    order: list[str] = []

    class FakeServer:
        def __init__(self, config: object) -> None:
            pass

        def run(self, sockets: list[socket.socket]) -> None:
            order.append("serve")

    monkeypatch.setattr(cli, "bind_service_ports", lambda s, svc: socket.socket())
    monkeypatch.setattr(cli, "build_service_app", lambda s, svc: object())
    monkeypatch.setattr(cli, "uvicorn_config", lambda app, s: object())
    monkeypatch.setattr(cli, "raise_open_file_limit", lambda: order.append("limit"))
    monkeypatch.setattr(cli, "request_foreground_role", lambda: order.append("role"))
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert cli.cmd_serve(settings, "tts") == 0
    assert order == ["limit", "role", "serve"]


@pytest.mark.skipif(sys.platform != "darwin", reason="task roles are a macOS policy")
def test_request_foreground_role_sets_the_task_role() -> None:
    # In a child process, so the test runner keeps its own role.
    code = (
        "import os; from voiced import cli; "
        "before = os.getpriority(cli.PRIO_DARWIN_ROLE, 0); "
        "cli.request_foreground_role(); "
        "print(before, os.getpriority(cli.PRIO_DARWIN_ROLE, 0))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["0", str(PRIO_DARWIN_ROLE_UI_FOCAL)]


@pytest.mark.parametrize("error", [AttributeError("no task_policy_set"), OSError("no libSystem")])
def test_request_foreground_role_failure_does_not_stop_serving(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def fail(name: object) -> None:
        raise error

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", fail)
    with caplog.at_level("WARNING", logger="voiced.cli"):
        cli.request_foreground_role()
    assert f"could not set the foreground task role: {error}" in caplog.text


class FakeLibc:
    """``task_policy_set`` and ``mach_task_self`` with a set return code."""

    def __init__(self, kr: int) -> None:
        self.mach_task_self = lambda: 7
        self.task_policy_set = lambda *args: kr


def test_request_foreground_role_logs_a_refused_task_policy_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda name: FakeLibc(kr=4))  # KERN_INVALID_ARGUMENT
    monkeypatch.setattr(os, "getpriority", lambda *a: pytest.fail("read back after a refusal"))
    with caplog.at_level("WARNING", logger="voiced.cli"):
        cli.request_foreground_role()
    assert "could not set the foreground task role: kern_return 4" in caplog.text


def test_request_foreground_role_survives_a_failed_read_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(which: int, who: int) -> int:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda name: FakeLibc(kr=0))
    monkeypatch.setattr(os, "getpriority", refuse)
    with caplog.at_level("INFO", logger="voiced.cli"):
        cli.request_foreground_role()
    assert "task role set to foreground application; read back failed" in caplog.text
    assert "could not set" not in caplog.text


def test_request_foreground_role_is_a_no_op_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getpriority", lambda *a: pytest.fail("called off macOS"))
    cli.request_foreground_role()
