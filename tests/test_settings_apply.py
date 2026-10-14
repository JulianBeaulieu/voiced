"""``voiced.config.apply_settings``: validation, atomic write, restart rules."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

import voiced.config as config
from voiced.config import (
    ConfigFileError,
    InvalidSetting,
    Settings,
    apply_settings,
    bind_mode,
    dump_toml,
    load_settings,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


@pytest.fixture
def cfg(settings: Settings, tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE.read_text())
    return path


def test_comments_and_other_keys_survive(cfg: Path) -> None:
    before = cfg.read_text()
    change = apply_settings({"tts.voice": "bf_emma", "browser_settings": True}, cfg)
    after = cfg.read_text()
    assert change.comments_preserved
    assert 'default_voice = "bf_emma"' in after
    assert "browser_settings = true" in after
    removed = set(before.splitlines()) - set(after.splitlines())
    assert removed == {'default_voice = "af_heart"', "browser_settings = false"}
    assert [line for line in after.splitlines() if line.startswith("#")] == [
        line for line in before.splitlines() if line.startswith("#")
    ]


def test_trailing_comment_kept(tmp_path: Path, settings: Settings) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[tts]\nspeed = 1.0   # "slow" # really\nengine = "fake"\n')
    apply_settings({"tts.speed": 1.25}, cfg)
    assert cfg.read_text() == '[tts]\nspeed = 1.25   # "slow" # really\nengine = "fake"\n'


def test_new_file_and_new_section(tmp_path: Path, settings: Settings) -> None:
    cfg = tmp_path / "sub" / "c.toml"
    change = apply_settings({"stt.model": "local/model"}, cfg)
    assert tomllib.loads(cfg.read_text()) == {"stt": {"model": "local/model"}}
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert change.restart_required == ("stt",)
    apply_settings({"zeroconf": False}, cfg)
    assert tomllib.loads(cfg.read_text()) == {
        "stt": {"model": "local/model"},
        "server": {"zeroconf": False},
    }


def test_unusual_layout_falls_back_to_rewrite(tmp_path: Path, settings: Settings) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('# gone\nserver.bind = "127.0.0.1"\ntts = { speed = 1.0 }\n')
    change = apply_settings({"tts.speed": 2.0, "bind": "lan"}, cfg)
    assert not change.comments_preserved
    assert tomllib.loads(cfg.read_text()) == {
        "server": {"bind": "0.0.0.0"},
        "tts": {"speed": 2.0},
    }


def test_mode_is_kept(cfg: Path) -> None:
    cfg.chmod(0o640)
    apply_settings({"zeroconf": False}, cfg)
    assert cfg.stat().st_mode & 0o777 == 0o640


def test_invalid_never_reaches_the_file(cfg: Path) -> None:
    before = cfg.read_text()
    for changes, field in [
        ({"tts.speed": -1}, "tts.speed"),
        ({"tts.speed": 1.5, "tts.voice": 3}, "tts.voice"),
        ({"bind": "everywhere"}, "bind"),
        ({"browser_settings": 1}, "browser_settings"),
        ({"api_key": "a\nb"}, "api_key"),
        ({"server.log_dir": "/"}, "server.log_dir"),
    ]:
        with pytest.raises(InvalidSetting) as info:
            apply_settings(changes, cfg)
        assert info.value.field == field
    assert cfg.read_text() == before
    assert not [p for p in cfg.parent.iterdir() if p.name.endswith(".tmp")]


def test_file_already_invalid_is_reported(tmp_path: Path, settings: Settings) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text("[tts]\nspeed = -3\n")
    with pytest.raises(InvalidSetting) as info:
        apply_settings({"zeroconf": False}, cfg)
    assert info.value.field == "tts.speed"


def test_malformed_file(tmp_path: Path, settings: Settings) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text("[tts\n")
    with pytest.raises(ConfigFileError):
        apply_settings({"zeroconf": False}, cfg)
    assert cfg.read_text() == "[tts\n"


def test_failed_rename_leaves_the_old_file(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = cfg.read_text()

    def fail(src: str, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", fail)
    with pytest.raises(ConfigFileError):
        apply_settings({"zeroconf": False}, cfg)
    assert cfg.read_text() == before
    assert sorted(p.name for p in cfg.parent.iterdir()) == ["config.toml"]


@pytest.mark.parametrize(
    ("changes", "restart"),
    [
        ({"bind": "lan"}, ("stt", "tts")),
        ({"api_key": "k"}, ("stt", "tts")),
        ({"zeroconf": False}, ("stt", "tts")),
        ({"browser_settings": True}, ()),
        ({"stt.engine": "fake", "stt.model": "m"}, ("stt",)),
        ({"tts.speed": 1.5}, ("tts",)),
        ({"tts.voice": "af_heart"}, ()),  # already the value
        ({"tts.speed": 1.5, "stt.model": "m"}, ("stt", "tts")),
    ],
)
def test_restart_required(cfg: Path, changes: dict[str, object], restart: tuple[str, ...]) -> None:
    assert apply_settings(changes, cfg).restart_required == restart


def test_bind_modes(cfg: Path) -> None:
    assert bind_mode("127.0.0.1") == "loopback"
    assert bind_mode("::1") == "loopback"
    assert bind_mode("localhost") == "loopback"
    assert bind_mode("0.0.0.0") == "lan"
    assert bind_mode("") == "lan"
    assert bind_mode("192.0.2.10") == "lan"
    change = apply_settings({"bind": "lan"}, cfg)
    assert change.settings.server.bind == "0.0.0.0"
    assert change.warnings == ("lan_without_api_key",)
    # A specific LAN address is kept when "lan" is asked for again.
    text = cfg.read_text().replace('bind = "0.0.0.0"', 'bind = "192.0.2.10"')
    cfg.write_text(text)
    assert apply_settings({"bind": "lan"}, cfg).changed == ()
    assert load_settings(cfg).server.bind == "192.0.2.10"
    assert apply_settings({"bind": "loopback"}, cfg).settings.server.bind == "127.0.0.1"


def test_api_key_round_trip(cfg: Path) -> None:
    change = apply_settings({"api_key": "s3cret", "bind": "lan"}, cfg)
    assert change.warnings == ()
    assert load_settings(cfg).server.api_key == "s3cret"
    assert apply_settings({"api_key": ""}, cfg).warnings == ("lan_without_api_key",)
    assert load_settings(cfg).server.api_key == ""


def test_env_override_is_reported(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICED_TTS__SPEED", "2")
    change = apply_settings({"tts.speed": 1.5}, cfg)
    assert change.warnings == ("env_override:tts.speed",)
    assert change.settings.tts.speed == 2.0
    assert tomllib.loads(cfg.read_text())["tts"]["speed"] == 1.5


def test_unknown_key(cfg: Path) -> None:
    with pytest.raises(InvalidSetting) as info:
        apply_settings({"stt.http_port": 1}, cfg)
    assert info.value.field == "stt.http_port"


def test_check_hook_can_refuse(cfg: Path) -> None:
    def refuse(sections: object, changing: set[str]) -> None:
        raise InvalidSetting("tts.engine", f"no ({sorted(changing)})")

    before = cfg.read_text()
    with pytest.raises(InvalidSetting, match=r"tts\.engine"):
        apply_settings({"tts.engine": "x"}, cfg, check=refuse)
    assert cfg.read_text() == before


def test_dump_toml_round_trips() -> None:
    data = {
        "server": {"bind": 'a"b\\c', "zeroconf": True, "session_idle_s": 1.5},
        "stt": {"languages": ["en", "de"], "queue_depth": 3},
        "x": {"y": {"z": "w"}},
    }
    assert tomllib.loads(dump_toml(data)) == data


def test_example_config_documents_browser_settings() -> None:
    assert tomllib.loads(EXAMPLE.read_text())["server"]["browser_settings"] is False
    assert os.path.exists(EXAMPLE)
