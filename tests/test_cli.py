from __future__ import annotations

import json

import pytest

from voiced.cli import main


def test_config_prints_effective_settings(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", "/nonexistent/voiced.toml")
    monkeypatch.setenv("VOICED_SERVER__BIND", "0.0.0.0")
    assert main(["config"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["server"]["bind"] == "0.0.0.0"
    assert payload["_config_file"] == "/nonexistent/voiced.toml"
