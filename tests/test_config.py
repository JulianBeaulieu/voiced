from __future__ import annotations

import os
from pathlib import Path

import pytest

from voiced.config import (
    DEFAULT_LAUNCHD_LABEL_PREFIX,
    ENGINE_ENV,
    Settings,
    apply_engine_environment,
    load_settings,
)


def test_defaults(settings: Settings) -> None:
    assert settings.server.bind == "127.0.0.1"
    assert settings.server.api_key == ""
    # The fixture moves every port to a free one; check the real defaults.
    assert Settings().stt.http_port == 8881
    assert Settings().stt.wyoming_port == 10300
    assert Settings().tts.http_port == 8880
    assert Settings().tts.wyoming_port == 10200
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


def test_engine_environment_comes_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the plists used to carry (ADR 0018): HF_HOME and the espeak-ng
    overrides, set from the config file, and only from it."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[install]\nhf_home = "~/hf"\n[tts]\nespeak_library = "/x/libespeak-ng.dylib"\n')
    settings = load_settings(cfg)
    assert settings.engine_environment() == {
        "HF_HOME": str(Path.home() / "hf"),
        "VOICED_ESPEAK_LIBRARY": "/x/libespeak-ng.dylib",
    }
    monkeypatch.setenv("VOICED_ESPEAK_LIBRARY", "/terminal/lib.dylib")
    monkeypatch.setenv("VOICED_ESPEAK_DATA", "/terminal/data")
    monkeypatch.setenv("HF_HOME", "/elsewhere")  # often exported for other tools: no notice
    assert apply_engine_environment(settings) == [
        "ignoring VOICED_ESPEAK_LIBRARY from the environment; "
        "set [tts] espeak_library in the config",
        "ignoring VOICED_ESPEAK_DATA from the environment; set [tts] espeak_data in the config",
    ]
    assert {k: os.environ.get(k) for k in ENGINE_ENV} == {
        "HF_HOME": str(Path.home() / "hf"),
        "VOICED_ESPEAK_LIBRARY": "/x/libespeak-ng.dylib",
        "VOICED_ESPEAK_DATA": None,  # the shell's is cleared
    }


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("server", "log_dir"),
        ("install", "hf_home"),
        ("tts", "espeak_library"),
        ("tts", "espeak_data"),
    ],
)
def test_paths_the_services_and_installer_share_must_be_absolute(
    tmp_path: Path, section: str, key: str
) -> None:
    """The services run in the home, the installer and its children anywhere:
    a relative path would mean two different places."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[{section}]\n{key} = "relative/dir"\n')
    with pytest.raises(ValueError, match="must be an absolute path"):
        load_settings(cfg)
    cfg.write_text(f'[{section}]\n{key} = "~/dir"\n')
    assert getattr(getattr(load_settings(cfg), section), key) == Path.home() / "dir"


@pytest.mark.parametrize("key", ["HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_ENDPOINT"])
def test_a_shell_hf_cache_or_mirror_is_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """Either would send the download somewhere the services do not read
    (HF_HUB_CACHE overrides HF_HOME), or fetch it from another endpoint."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[install]\nhf_home = "/hf"\n')
    monkeypatch.setenv(key, "/elsewhere")
    assert apply_engine_environment(load_settings(cfg)) == []
    assert key not in os.environ
    assert os.environ["HF_HOME"] == "/hf"


def test_no_notice_when_the_shell_agrees_with_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[tts]\nespeak_library = "/x/libespeak-ng.dylib"\n')
    monkeypatch.setenv("VOICED_ESPEAK_LIBRARY", "/x/libespeak-ng.dylib")
    assert apply_engine_environment(load_settings(cfg)) == []
