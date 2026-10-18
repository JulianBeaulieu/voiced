"""``voiced serve`` with a configured port already taken, through the real CLI
in a subprocess.

The serve path must refuse before it builds the app, so a clash never starts a
model load and never exits 0. The subprocess uses the fake engine, needs no
MLX, and runs on CI.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from voiced.cli import PORT_IN_USE_EXIT

# Interpreter start-up and imports only: the check runs before the app (and
# the engine) is built, so there is nothing to wait for.
FAIL_FAST_S = 5.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


@contextlib.contextmanager
def _hold(port: int) -> Iterator[None]:
    """Keep ``port`` bound and listening for the duration, like a running service."""
    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", port))
    held.listen()
    try:
        yield
    finally:
        held.close()


@pytest.mark.parametrize("service", ["stt", "tts"])
@pytest.mark.parametrize("clash", ["HTTP", "Wyoming"])
def test_serve_exits_non_zero_before_loading_when_a_port_is_in_use(
    service: str, clash: str, tmp_path: os.PathLike[str]
) -> None:
    http_port, wyoming_port = _free_port(), _free_port()
    held = http_port if clash == "HTTP" else wyoming_port
    section = service.upper()
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICED_")}
    env |= {
        "VOICED_CONFIG": os.path.join(tmp_path, "missing.toml"),
        "VOICED_SERVER__ZEROCONF": "false",
        "VOICED_SERVER__BIND": "127.0.0.1",
        "VOICED_SERVER__LOG_DIR": os.path.join(tmp_path, "logs"),
        f"VOICED_{section}__ENGINE": "fake",
        f"VOICED_{section}__HTTP_PORT": str(http_port),
        f"VOICED_{section}__WYOMING_PORT": str(wyoming_port),
    }
    with _hold(held):
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "voiced.cli", "serve", service],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        elapsed = time.monotonic() - started
    assert proc.returncode == PORT_IN_USE_EXIT, f"rc={proc.returncode} stderr={proc.stderr!r}"
    assert f"{clash} port {held} on 127.0.0.1 is already in use" in proc.stderr, proc.stderr
    assert elapsed < FAIL_FAST_S, f"serve took {elapsed:.1f}s to refuse"
    # Nothing was configured or loaded: no log file was even opened.
    assert not os.path.exists(os.path.join(tmp_path, "logs", f"{service}.log"))


def test_main_sets_the_engine_environment_from_the_config_before_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """huggingface_hub reads HF_HOME when it is imported, so main() sets the
    config's before any command runs, and clears what the shell exported:
    serve, doctor and the installer's download all see the same."""
    from voiced import cli
    from voiced.config import Settings

    config = tmp_path / "config.toml"
    config.write_text(f'[install]\nhf_home = "{tmp_path / "hf"}"\n')
    monkeypatch.setenv("VOICED_CONFIG", str(config))
    monkeypatch.setenv("VOICED_ESPEAK_LIBRARY", "/from/the/shell.dylib")
    seen: list[tuple[str | None, str | None]] = []

    def bind(settings: Settings, service: str) -> socket.socket:
        seen.append((os.environ.get("HF_HOME"), os.environ.get("VOICED_ESPEAK_LIBRARY")))
        raise cli.PortInUse("HTTP", "127.0.0.1", 1)

    monkeypatch.setattr(cli, "bind_service_ports", bind)
    assert cli.main(["serve", "tts"]) == PORT_IN_USE_EXIT
    assert seen == [(str(tmp_path / "hf"), None)]
    assert "voiced: ignoring VOICED_ESPEAK_LIBRARY" in capsys.readouterr().err
