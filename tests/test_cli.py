from __future__ import annotations

import json
import resource
import socket

import pytest
import uvicorn

from voiced import cli
from voiced.cli import main
from voiced.config import Settings


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


def test_serve_raises_the_limit_before_serving(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The service process raises its own limit once, before uvicorn accepts
    a connection; the app (and so every test that runs a lifespan) does not."""
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
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert cli.cmd_serve(settings, "tts") == 0
    assert order == ["limit", "serve"]
