"""Fixtures for the control channel: a fake supervisor and short socket paths."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

from voiced.config import Settings, load_settings
from voiced.control.ops import NotInstalled, Operations


class FakeSupervisor:
    def __init__(self) -> None:
        self.state = "running"
        self.restarted: list[str] = []
        self.changed: list[Settings] = []
        self.installed = True

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

    def restart(self, service: Literal["stt", "tts"]) -> str:
        if not self.installed:
            raise NotInstalled(f"{service} is not loaded in launchd; nothing to restart")
        self.restarted.append(service)
        return f"restarted {service}"

    def settings_changed(self, settings: Settings) -> None:
        self.changed.append(settings)


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
    return Operations(supervisor, config_file, load_settings)
