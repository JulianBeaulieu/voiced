from __future__ import annotations

import json

import pytest

from voiced.cli import main


@pytest.mark.parametrize(
    "cmd", ["install", "start", "stop", "status", "logs", "uninstall", "doctor"]
)
def test_lifecycle_commands_are_stubbed(cmd: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([cmd]) == 2
    assert "not implemented" in capsys.readouterr().err


def test_config_prints_effective_settings(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", "/nonexistent/voiced.toml")
    monkeypatch.setenv("VOICED_SERVER__BIND", "0.0.0.0")
    assert main(["config"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["server"]["bind"] == "0.0.0.0"
    assert payload["_config_file"] == "/nonexistent/voiced.toml"
