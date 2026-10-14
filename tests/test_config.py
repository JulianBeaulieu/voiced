from __future__ import annotations

from pathlib import Path

import pytest

from voiced.config import DEFAULT_LAUNCHD_LABEL_PREFIX, Settings, load_settings


def test_defaults(settings: Settings) -> None:
    assert settings.server.bind == "127.0.0.1"
    assert settings.server.api_key == ""
    assert settings.stt.http_port == 8881
    assert settings.stt.wyoming_port == 10300
    assert settings.tts.http_port == 8880
    assert settings.tts.wyoming_port == 10200
    assert settings.tts.default_voice == "af_heart"
    assert settings.stt.vad_silence_ms == 200
    assert settings.stt.languages == ["en"]
    assert settings.launchd.label_prefix == DEFAULT_LAUNCHD_LABEL_PREFIX
    assert settings.launchd.label("stt") == "io.github.julianbeaulieu.voiced.stt"
    assert settings.launchd.label("watchdog") == "io.github.julianbeaulieu.voiced.watchdog"


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nope.toml")
    assert s.tts.http_port == 8880


def test_file_values_and_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[server]\nbind = "0.0.0.0"\n[tts]\nhttp_port = 9000\ndefault_voice = "bf_emma"\n'
        '[launchd]\nlabel_prefix = "computer.voiced"\n'
    )
    monkeypatch.setenv("VOICED_TTS__HTTP_PORT", "9001")
    monkeypatch.setenv("VOICED_STT__LANGUAGES", '["en", "de"]')
    s = load_settings(cfg)
    assert s.server.bind == "0.0.0.0"
    assert s.tts.default_voice == "bf_emma"  # from file
    assert s.tts.http_port == 9001  # env beats file
    assert s.stt.languages == ["en", "de"]  # env, JSON list
    assert s.launchd.label("tts") == "computer.voiced.tts"


def test_env_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "elsewhere.toml"
    cfg.write_text("[stt]\nqueue_depth = 3\n")
    monkeypatch.setenv("VOICED_CONFIG", str(cfg))
    assert load_settings().stt.queue_depth == 3


def test_unknown_key_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[server]\nbnid = 1\n")
    with pytest.raises(ValueError):
        load_settings(cfg)


def test_example_config_matches_defaults(settings: Settings) -> None:
    """config.example.toml documents the defaults, so loading it must change nothing."""
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    assert load_settings(example) == Settings()
