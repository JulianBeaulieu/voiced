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

from voiced.cli import ENGINE_REFUSED_EXIT, PORT_IN_USE_EXIT

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


@pytest.mark.parametrize(
    ("engine", "model", "says"),
    [
        ("phonon", "gradium/phonon-40m-fr", "is not in voiced's model catalogue"),
        ("nope", "org/m", "unknown tts engine 'nope'"),
    ],
)
def test_serve_says_in_one_line_why_the_engine_cannot_be_built(
    engine: str, model: str, says: str, tmp_path: os.PathLike[str]
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICED_")}
    env |= {
        "VOICED_CONFIG": os.path.join(tmp_path, "missing.toml"),
        "VOICED_SERVER__ZEROCONF": "false",
        "VOICED_SERVER__BIND": "127.0.0.1",
        "VOICED_SERVER__LOG_DIR": os.path.join(tmp_path, "logs"),
        "VOICED_TTS__ENGINE": engine,
        "VOICED_TTS__MODEL": model,
        "VOICED_TTS__HTTP_PORT": str(_free_port()),
        "VOICED_TTS__WYOMING_PORT": str(_free_port()),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "voiced.cli", "serve", "tts"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == ENGINE_REFUSED_EXIT, proc.stderr
    assert "Traceback" not in proc.stderr
    assert (
        [line for line in proc.stderr.splitlines() if line.startswith("voiced serve")]
        == [line for line in proc.stderr.splitlines() if says in line]
        != []
    )


def test_serve_runs_the_services_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """They load from the cache and never download a model (ADR 0022)."""
    from voiced import cli
    from voiced.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")  # restored after the test
    seen: list[str | None] = []

    def bind(settings: Settings, service: str) -> socket.socket:
        seen.append(os.environ.get("HF_HUB_OFFLINE"))
        raise cli.PortInUse("HTTP", "127.0.0.1", 1)

    monkeypatch.setattr(cli, "bind_service_ports", bind)
    assert cli.cmd_serve(Settings(), "tts") == PORT_IN_USE_EXIT
    assert seen == ["1"]


@pytest.mark.parametrize("service", ["stt", "tts"])
def test_a_service_switched_off_parks_without_ports_until_sigterm(
    service: str, tmp_path: Path
) -> None:
    """``[<service>] enabled = false``: no engine, no port, the pid in the
    marker, and a clean exit on SIGTERM that removes it (ADR 0033)."""
    import signal

    from voiced.launchd import parked

    http_port, wyoming_port = _free_port(), _free_port()
    state = tmp_path / "state"
    section = service.upper()
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICED_")}
    env |= {
        "VOICED_CONFIG": os.path.join(tmp_path, "missing.toml"),
        "VOICED_SERVER__LOG_DIR": os.path.join(tmp_path, "logs"),
        "VOICED_INSTALL__VENV_DIR": str(state),
        f"VOICED_{section}__ENABLED": "false",
        f"VOICED_{section}__ENGINE": "no-such-engine",  # never built
        f"VOICED_{section}__HTTP_PORT": str(http_port),
        f"VOICED_{section}__WYOMING_PORT": str(wyoming_port),
    }
    marker = parked.marker(state, service)
    proc = subprocess.Popen(
        [sys.executable, "-m", "voiced.cli", "serve", service],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while parked.parked_pid(marker) != proc.pid:
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "no marker"
            time.sleep(0.05)
        assert parked.is_parked(marker, proc.pid)
        for port in (http_port, wyoming_port):
            with socket.socket() as s:
                assert s.connect_ex(("127.0.0.1", port)) != 0, f"port {port} answers"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
    assert not marker.exists()
    log = (tmp_path / "logs" / f"{service}.log").read_text()
    assert f"off ([{service}] enabled = false)" in log


def test_a_serving_process_clears_an_old_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from voiced import cli
    from voiced.config import Settings
    from voiced.launchd import parked

    settings = Settings()
    settings.install.venv_dir = tmp_path
    marker = parked.marker(tmp_path, "tts")
    marker.write_text("12345\n")

    def bind(settings: Settings, service: str) -> socket.socket:
        raise cli.PortInUse("HTTP", "127.0.0.1", 1)

    monkeypatch.setattr(cli, "bind_service_ports", bind)
    assert cli.cmd_serve(settings, "tts") == PORT_IN_USE_EXIT
    assert not marker.exists()
