"""``voiced.settings_store.apply_settings``: validation, atomic write, restart rules."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

import voiced.settings_store as store
from voiced.config import Settings, load_settings
from voiced.settings_store import (
    ConfigFileError,
    InvalidSetting,
    apply_settings,
    bind_mode,
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
    assert change.changed == ("tts.voice", "browser_settings")
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


def test_default_valued_key_on_a_minimal_file(tmp_path: Path, settings: Settings) -> None:
    """A requested key absent from the file and equal to its default is
    written along with the real change, so the file reads back as written."""
    cfg = tmp_path / "c.toml"
    cfg.write_text('[tts]\nengine = "fake"\n')
    change = apply_settings({"tts.speed": 1.0, "zeroconf": False}, cfg)
    assert change.changed == ("zeroconf",)
    assert change.restart_required == ("stt", "tts")
    assert tomllib.loads(cfg.read_text()) == {
        "tts": {"engine": "fake", "speed": 1.0},
        "server": {"zeroconf": False},
    }


def test_only_default_valued_keys_leave_the_file_alone(tmp_path: Path, settings: Settings) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[tts]\nengine = "fake"\n')
    change = apply_settings({"tts.speed": 1.0, "zeroconf": True}, cfg)
    assert change.changed == ()
    assert cfg.read_text() == '[tts]\nengine = "fake"\n'


def test_dotted_keys_and_inline_tables_keep_comments(tmp_path: Path, settings: Settings) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('# kept\nserver.bind = "127.0.0.1"  # mine\ntts = { speed = 1.0 }\n')
    apply_settings({"tts.speed": 2.0, "bind": "lan"}, cfg)
    text = cfg.read_text()
    assert text.startswith("# kept\n")
    assert "# mine" in text
    assert tomllib.loads(text) == {"server": {"bind": "0.0.0.0"}, "tts": {"speed": 2.0}}


def test_edit_that_does_not_read_back_is_not_written(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = cfg.read_text()
    monkeypatch.setattr(store.tomlkit, "dumps", lambda doc: "[tts]\nspeed = 3.0\n")
    with pytest.raises(ConfigFileError, match="does not read back"):
        apply_settings({"tts.speed": 1.5}, cfg)
    assert cfg.read_text() == before


def test_mode_is_kept(cfg: Path) -> None:
    cfg.chmod(0o640)
    apply_settings({"zeroconf": False}, cfg)
    assert cfg.stat().st_mode & 0o777 == 0o640


def test_a_file_with_the_key_becomes_private(cfg: Path) -> None:
    cfg.chmod(0o644)
    apply_settings({"api_key": "s3cret"}, cfg)
    assert cfg.stat().st_mode & 0o777 == 0o600


def test_symlinked_config_stays_a_symlink(tmp_path: Path, settings: Settings) -> None:
    real = tmp_path / "real" / "c.toml"
    real.parent.mkdir()
    real.write_text("[tts]\nspeed = 1.0\n")
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    apply_settings({"tts.speed": 1.5}, link)
    assert link.is_symlink()
    assert tomllib.loads(real.read_text()) == {"tts": {"speed": 1.5}}
    assert sorted(p.name for p in real.parent.iterdir()) == ["c.toml"]


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

    monkeypatch.setattr(store.os, "replace", fail)
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


ENGINES = {
    "stt": [
        {"name": "fake", "label": "Fake", "models": ["fake/stt"], "languages": ["en"]},
        {"name": "parakeet", "label": "Parakeet", "models": ["p/m"], "languages": ["en"]},
        {"name": "bare", "label": "Bare", "models": [], "languages": ["en"]},
    ],
    "tts": [
        {
            "name": "kokoro",
            "label": "Kokoro",
            "models": ["k/m"],
            "voices": ["af_alloy", "af_heart", "bf_emma"],
        },
        {"name": "soprano", "label": "Soprano", "models": ["s/m"], "voices": ["default"]},
        {"name": "open", "label": "Open", "models": ["o/m"], "voices": []},
    ],
}


def engines() -> dict[str, object]:
    return ENGINES


def test_engine_change_takes_the_engine_model_and_voice(cfg: Path) -> None:
    change = apply_settings({"tts.engine": "soprano"}, cfg, engines=engines)
    assert change.changed == ("tts.engine", "tts.model", "tts.voice")
    tts = tomllib.loads(cfg.read_text())["tts"]
    assert (tts["engine"], tts["model"], tts["default_voice"]) == ("soprano", "s/m", "default")
    # Back to kokoro: its model again, and the built-in default voice rather
    # than its first one.
    apply_settings({"tts.engine": "kokoro"}, cfg, engines=engines)
    tts = tomllib.loads(cfg.read_text())["tts"]
    assert (tts["model"], tts["default_voice"]) == ("k/m", "af_heart")
    apply_settings({"stt.engine": "fake"}, cfg, engines=engines)
    assert tomllib.loads(cfg.read_text())["stt"]["model"] == "fake/stt"


def test_engine_change_keeps_what_was_given(cfg: Path) -> None:
    changes = {"tts.engine": "kokoro", "tts.model": "mine/m", "tts.voice": "bf_emma"}
    apply_settings({"tts.engine": "soprano"}, cfg, engines=engines)
    apply_settings(changes, cfg, engines=engines)
    tts = tomllib.loads(cfg.read_text())["tts"]
    assert (tts["model"], tts["default_voice"]) == ("mine/m", "bf_emma")
    # An engine that lists no voices keeps the voice.
    apply_settings({"tts.engine": "open"}, cfg, engines=engines)
    assert tomllib.loads(cfg.read_text())["tts"]["default_voice"] == "bf_emma"


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"tts.engine": "nope"}, "tts.engine", "'nope' is not installed (kokoro, open, soprano)"),
        ({"stt.engine": "bare"}, "stt.model", "set stt.model in the same change"),
        ({"tts.voice": "default"}, "tts.voice", "kokoro has no voice 'default'"),
        ({"tts.engine": "soprano", "tts.voice": "af_heart"}, "tts.voice", "soprano has no"),
    ],
)
def test_engine_checks_refuse(
    cfg: Path, changes: dict[str, object], field: str, message: str
) -> None:
    before = cfg.read_text()
    with pytest.raises(InvalidSetting) as info:
        apply_settings(changes, cfg, engines=engines)
    assert info.value.field == field
    assert info.value.message.startswith(message)
    assert cfg.read_text() == before


def test_catalogue_is_only_read_for_engine_changes(cfg: Path) -> None:
    def boom() -> dict[str, object]:
        raise AssertionError("catalogue read")

    apply_settings({"tts.speed": 1.5, "tts.model": "x/y"}, cfg, engines=boom)


def test_example_config_documents_browser_settings() -> None:
    assert tomllib.loads(EXAMPLE.read_text())["server"]["browser_settings"] is False
    assert os.path.exists(EXAMPLE)


# A model that is not downloaded (ADR 0022) ----------------------------------------

PHONON_40M = "gradium/phonon-40m-en"


def test_a_catalogued_model_that_is_not_downloaded_is_refused(cfg: Path, model_cache: Path) -> None:
    """Even without the engine catalogue, every time from the disk, and
    nothing is written."""
    before = cfg.read_text()
    with pytest.raises(store.ModelMissing) as exc:
        apply_settings({"tts.engine": "phonon", "tts.model": PHONON_40M}, cfg)
    assert (exc.value.field, exc.value.model) == ("tts.model", PHONON_40M)
    assert exc.value.message == (
        f"{PHONON_40M} is not downloaded; run voiced models download {PHONON_40M}, then "
        f"voiced settings set tts.engine=phonon tts.model={PHONON_40M}"
    )
    assert isinstance(exc.value, InvalidSetting)  # callers that know only that still refuse
    assert cfg.read_text() == before
    # The model alone needs its engine's shared voices too.
    from tests import hf_cache_support as hf
    from voiced import models

    entry = models.lock()[PHONON_40M]
    hf.add_locked(model_cache, entry)
    with pytest.raises(store.ModelMissing):
        apply_settings({"tts.engine": "phonon", "tts.model": PHONON_40M}, cfg)
    voices = models.lock()["gradium/phonon-voices"]
    hf.add_locked(model_cache, voices)
    change = apply_settings({"tts.engine": "phonon", "tts.model": PHONON_40M}, cfg)
    assert change.changed == ("tts.engine", "tts.model")


def test_the_model_an_engine_switch_implies_is_checked(cfg: Path) -> None:
    catalogue = {"stt": [], "tts": [{"name": "phonon", "models": [PHONON_40M], "voices": []}]}
    with pytest.raises(store.ModelMissing) as exc:
        apply_settings({"tts.engine": "phonon"}, cfg, engines=lambda: catalogue)
    assert exc.value.model == PHONON_40M


def test_a_model_outside_the_catalogue_and_other_keys_are_not_checked(cfg: Path) -> None:
    apply_settings({"tts.model": "someone/own-model"}, cfg)
    apply_settings({"tts.speed": 1.5}, cfg)  # the configured default is not downloaded either


# enabled and idle_unload_s (ADR 0033) ------------------------------------------


def test_switching_a_service_off_restarts_it(cfg: Path) -> None:
    change = apply_settings({"stt.enabled": False, "tts.idle_unload_s": 300}, cfg)
    assert change.changed == ("stt.enabled", "tts.idle_unload_s")
    assert change.restart_required == ("stt", "tts")
    settings = load_settings(cfg)
    assert settings.stt.enabled is False
    assert settings.tts.idle_unload_s == 300
    assert store.settings_view(settings, cfg)["stt"]["enabled"] is False
    assert store.settings_view(settings, cfg)["tts"]["idle_unload_s"] == 300


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("stt.enabled", "false"),
        ("tts.enabled", 0),
        ("stt.idle_unload_s", -1),
        ("tts.idle_unload_s", 1.5),
        ("tts.idle_unload_s", True),
        ("stt.idle_unload_s", "300"),
    ],
)
def test_enabled_and_idle_unload_are_checked(cfg: Path, name: str, value: object) -> None:
    before = cfg.read_text()
    with pytest.raises(InvalidSetting) as info:
        apply_settings({name: value}, cfg)
    assert info.value.field == name
    assert cfg.read_text() == before
