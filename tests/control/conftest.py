"""Fixtures for the control channel: a fake supervisor and short socket paths."""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

from voiced.config import Settings
from voiced.control.ops import NotInstalled, Operations, ServiceUnavailable
from voiced.engines.catalogue import engines_list


class FakeSupervisor:
    def __init__(self) -> None:
        self.state = "running"
        self.restarted: list[str] = []
        self.installed = True
        # service -> "not_installed" or "unavailable": check_restart refuses it.
        self.refuse: dict[str, str] = {}
        # Cleared, restart() blocks until it is set (a watchdog mid-poll).
        self.restart_gate = threading.Event()
        self.restart_gate.set()
        # What speak() raises instead of speaking, and what it was asked.
        self.speech_error: Exception | None = None
        self.spoken: list[tuple[str, str]] = []

    def status(self) -> dict[str, Any]:
        service = {
            "state": self.state,
            "pid": 123,
            "health": None,
            "engine": "fake",
            "model": "fake/tts",
            "warm": True,
            "uptime_s": 1.0,
            "restarts": len(self.restarted),
        }
        return {
            "installed": self.installed,
            "services": {"stt": dict(service), "tts": dict(service), "watchdog": dict(service)},
        }

    def check_restart(self, service: Literal["stt", "tts"]) -> None:
        refusal = self.refuse.get(service)
        if refusal == "not_installed":
            raise NotInstalled(f"{service} has no launchd plist")
        if refusal == "unavailable":
            raise ServiceUnavailable(f"{service} is installed but not loaded in launchd")

    def restart(self, service: Literal["stt", "tts"]) -> str:
        self.restart_gate.wait(10)
        self.restarted.append(service)
        return f"restarted {service}"

    def speak(self, text: str, voice: str) -> bytes:
        self.spoken.append((text, voice))
        if self.speech_error is not None:
            raise self.speech_error
        return b"\x01\x00" * 2400  # 0.1 s at 24 kHz


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """Unix socket paths are limited to about 104 bytes; pytest's tmp_path is
    often longer, so sockets go under a short directory in /tmp."""
    path = Path(tempfile.mkdtemp(prefix="vc", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def config_file(settings: Settings, tmp_path: Path) -> Path:
    """A config file using the fake engines and the fixture's log dir."""
    path = tmp_path / "config.toml"
    path.write_text(
        "# kept\n"
        "[server]\n"
        f'log_dir = "{settings.server.log_dir}"\n'
        "\n[stt]\n"
        'engine = "fake"\n'
        "\n[tts]\n"
        'engine = "fake"  # the fake engine\n'
    )
    return path


@pytest.fixture
def supervisor() -> FakeSupervisor:
    return FakeSupervisor()


@pytest.fixture
def ops(supervisor: FakeSupervisor, config_file: Path) -> Operations:
    return Operations(supervisor, config_file, engines=engines_list)
