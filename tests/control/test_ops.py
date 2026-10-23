"""Operation dispatch, without a socket."""

from __future__ import annotations

import asyncio
import base64
import json
import tomllib
from pathlib import Path

import pytest

from voiced.control import ops as ops_module
from voiced.control import protocol
from voiced.control.ops import CatalogueUnavailable, Operations, engines_in_subprocess
from voiced.control.protocol import OpError
from voiced.control.speech import MAX_TEXT_CHARS, SpeechRefused, SpeechUnavailable

from .conftest import FakeSupervisor


def read(path: Path) -> str:
    return path.read_text()


async def test_dispatch_unknown_op(ops: Operations) -> None:
    with pytest.raises(OpError) as info:
        await ops.dispatch("nope", {})
    assert info.value.code == protocol.UNKNOWN_OP


async def test_dispatch_status(ops: Operations) -> None:
    result = await ops.dispatch("status", {})
    assert result["installed"] is True
    assert set(result["services"]) == {"stt", "tts", "watchdog"}


async def test_dispatch_internal_error(ops: Operations, supervisor: FakeSupervisor) -> None:
    def boom() -> dict[str, object]:
        raise RuntimeError("bug")

    supervisor.status = boom  # type: ignore[method-assign]
    with pytest.raises(OpError) as info:
        await ops.dispatch("status", {})
    assert info.value.code == protocol.INTERNAL


async def test_restart(ops: Operations, supervisor: FakeSupervisor) -> None:
    result = await ops.dispatch("service.restart", {"service": "tts"})
    assert result == {"accepted": True, "detail": {"tts": "restarting"}}
    await ops.wait_restarts()
    await ops.dispatch("service.restart", {"service": "all"})
    await ops.wait_restarts()
    assert sorted(supervisor.restarted) == ["stt", "tts", "tts"]
    for bad in ({}, {"service": "watchdog"}, {"service": 1}):
        with pytest.raises(OpError) as info:
            await ops.dispatch("service.restart", bad)
        assert info.value.code == protocol.BAD_REQUEST


async def test_restart_answers_before_the_restart_finishes(
    ops: Operations, supervisor: FakeSupervisor
) -> None:
    """The watchdog may be mid-poll for many seconds; the answer must not wait."""
    supervisor.restart_gate.clear()
    result = await asyncio.wait_for(ops.dispatch("service.restart", {"service": "stt"}), 2.0)
    assert result["accepted"] is True
    assert supervisor.restarted == []
    supervisor.restart_gate.set()
    await ops.wait_restarts()
    assert supervisor.restarted == ["stt"]


async def test_restart_not_installed(ops: Operations, supervisor: FakeSupervisor) -> None:
    supervisor.refuse = {"stt": "not_installed", "tts": "not_installed"}
    with pytest.raises(OpError) as info:
        await ops.dispatch("service.restart", {"service": "stt"})
    assert info.value.code == protocol.NOT_INSTALLED
    with pytest.raises(OpError) as info:
        await ops.dispatch("service.restart", {"service": "all"})
    assert info.value.code == protocol.NOT_INSTALLED


async def test_restart_installed_but_not_loaded(
    ops: Operations, supervisor: FakeSupervisor
) -> None:
    supervisor.refuse = {"tts": "unavailable"}
    with pytest.raises(OpError) as info:
        await ops.dispatch("service.restart", {"service": "tts"})
    assert info.value.code == protocol.SERVICE_UNAVAILABLE


async def test_restart_all_tries_both(ops: Operations, supervisor: FakeSupervisor) -> None:
    supervisor.refuse = {"stt": "unavailable"}
    result = await ops.dispatch("service.restart", {"service": "all"})
    await ops.wait_restarts()
    assert result["accepted"] is True
    assert result["detail"]["tts"] == "restarting"
    assert result["detail"]["stt"].startswith("service_unavailable: stt:")
    assert supervisor.restarted == ["tts"]
    supervisor.refuse = {"stt": "unavailable", "tts": "not_installed"}
    with pytest.raises(OpError) as info:
        await ops.dispatch("service.restart", {"service": "all"})
    assert info.value.code == protocol.SERVICE_UNAVAILABLE
    assert "stt:" in info.value.message and "tts:" in info.value.message


async def test_logs_tail(ops: Operations, config_file: Path) -> None:
    from voiced.config import load_settings

    log_dir = load_settings(config_file).server.log_dir
    log_dir.mkdir(parents=True)
    (log_dir / "tts.log").write_text("".join(f"line {i}\n" for i in range(50)))
    result = await ops.dispatch("logs.tail", {"service": "tts", "lines": 3})
    assert result["lines"] == ["line 47", "line 48", "line 49"]
    assert (await ops.dispatch("logs.tail", {"service": "stt"}))["lines"] == []
    for bad in (
        {"service": "x"},
        {"service": "tts", "lines": 1001},
        {"service": "tts", "lines": 0},
    ):
        with pytest.raises(OpError) as info:
            await ops.dispatch("logs.tail", bad)
        assert info.value.code == protocol.BAD_REQUEST


async def test_open(ops: Operations, config_file: Path) -> None:
    assert (await ops.dispatch("open", {"what": "config_file"}))["target"] == str(config_file)
    page = (await ops.dispatch("open", {"what": "status_page"}))["target"]
    assert page == "http://127.0.0.1:8880/"
    stt = await ops.dispatch("open", {"what": "status_page", "service": "stt"})
    assert stt["target"] == "http://127.0.0.1:8881/"
    assert (await ops.dispatch("open", {"what": "logs_dir"}))["target"].endswith("logs")
    with pytest.raises(OpError):
        await ops.dispatch("open", {"what": "terminal"})


async def test_settings_get_never_returns_the_key(ops: Operations, config_file: Path) -> None:
    await ops.dispatch("settings.set", {"api_key": "hunter2"})
    result = await ops.dispatch("settings.get", {})
    assert result["api_key_set"] is True
    assert "hunter2" not in json.dumps(result)
    assert "hunter2" in read(config_file)


async def test_settings_set_result(ops: Operations) -> None:
    result = await ops.dispatch("settings.set", {"tts.voice": "bf_emma", "tts.speed": 1.5})
    assert result["tts"]["voice"] == "bf_emma"
    assert result["tts"]["speed"] == 1.5
    assert result["changed"] == ["tts.voice", "tts.speed"]
    assert result["restart_required"] == ["tts"]
    assert result["warnings"] == []
    again = await ops.dispatch("settings.set", {"tts.voice": "bf_emma"})
    assert again["changed"] == []
    assert again["restart_required"] == []


@pytest.mark.usefixtures("models_downloaded")
async def test_engine_switch_takes_the_engine_model(ops: Operations, config_file: Path) -> None:
    result = await ops.dispatch("settings.set", {"tts.engine": "soprano"})
    assert result["changed"] == ["tts.engine", "tts.model", "tts.voice"]
    assert result["tts"]["voice"] == "default"
    from voiced.engines.soprano import SopranoEngine

    assert tomllib.loads(read(config_file))["tts"]["model"] == SopranoEngine.model_id


@pytest.mark.usefixtures("models_downloaded")
async def test_engine_switch_on_a_minimal_config(ops: Operations, config_file: Path) -> None:
    """Kokoro's model is the config default; it is written all the same."""
    from voiced.engines.kokoro import KokoroEngine

    await asyncio.to_thread(config_file.write_text, '[tts]\nengine = "fake"\n')
    result = await ops.dispatch("settings.set", {"tts.engine": "kokoro"})
    assert result["tts"]["engine"] == "kokoro"
    assert result["tts"]["model"] == KokoroEngine.model_id
    tts = tomllib.loads(read(config_file))["tts"]
    assert (tts["engine"], tts["model"]) == ("kokoro", KokoroEngine.model_id)


async def test_a_model_that_is_not_downloaded_has_its_own_code(
    ops: Operations, config_file: Path
) -> None:
    model = "gradium/phonon-40m-en"
    with pytest.raises(OpError) as info:
        await ops.dispatch("settings.set", {"tts.engine": "phonon", "tts.model": model})
    assert info.value.code == protocol.MODEL_NOT_DOWNLOADED == "model_not_downloaded"
    assert info.value.extra == {"field": "tts.model", "model": model}
    assert info.value.message.startswith(f"{model} is not downloaded; run voiced models download")


async def test_config_error_names_the_file(ops: Operations, config_file: Path) -> None:
    await asyncio.to_thread(config_file.write_text, "[tts\n")
    with pytest.raises(OpError) as info:
        await ops.dispatch("settings.get", {})
    assert info.value.code == protocol.INTERNAL
    assert info.value.message.startswith(f"config error at {config_file}:")
    with pytest.raises(OpError) as info:  # the same wording when setting
        await ops.dispatch("settings.set", {"tts.speed": 1.5})
    assert info.value.code == protocol.INTERNAL
    assert info.value.message.startswith(f"config error at {config_file}:")


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"tts.speed": 0}, "tts.speed"),
        ({"tts.speed": "fast"}, "tts.speed"),
        ({"bind": "0.0.0.0"}, "bind"),
        ({"zeroconf": "yes"}, "zeroconf"),
        ({"tts.voice": "nobody"}, "tts.voice"),
        ({"tts.engine": "missing"}, "tts.engine"),
        ({"stt.engine": "missing"}, "stt.engine"),
        ({"stt.model": "  "}, "stt.model"),
        ({"log_level": "debug"}, "log_level"),
    ],
)
async def test_settings_set_invalid(
    ops: Operations, config_file: Path, args: dict[str, object], field: str
) -> None:
    before = read(config_file)
    with pytest.raises(OpError) as info:
        await ops.dispatch("settings.set", args)
    assert info.value.code == protocol.INVALID_SETTING
    assert info.value.extra["field"] == field
    assert read(config_file) == before


async def test_settings_set_empty(ops: Operations) -> None:
    with pytest.raises(OpError) as info:
        await ops.dispatch("settings.set", {})
    assert info.value.code == protocol.BAD_REQUEST


async def test_engines_list(ops: Operations) -> None:
    result = await ops.dispatch("engines.list", {})
    tts = {e["name"]: e for e in result["tts"]}
    stt = {e["name"]: e for e in result["stt"]}
    assert {"fake", "kokoro", "soprano"} <= set(tts)
    assert {"fake", "parakeet"} <= set(stt)
    assert tts["fake"]["voices"] == ["af_heart", "am_adam", "bf_emma"]
    assert tts["soprano"]["voices"] == ["default"]
    assert tts["soprano"]["voice_labels"] == {"default": "Default"}
    assert tts["kokoro"]["label"] == "Kokoro"
    assert tts["kokoro"]["voice_labels"]["bf_emma"] == "Emma (British English, female)"
    assert stt["parakeet"]["label"] == "Parakeet"
    assert "en" in stt["parakeet"]["languages"]
    assert stt["parakeet"]["models"] == ["mlx-community/parakeet-tdt-0.6b-v3"]
    assert stt["nemotron"]["model_languages"]["nvidia/nemotron-speech-streaming-en-0.6b"] == [
        "en-US"
    ]


async def test_engines_list_is_cached(supervisor: FakeSupervisor, config_file: Path) -> None:
    calls: list[int] = []
    now = [0.0]

    def source() -> dict[str, object]:
        calls.append(1)
        return {"stt": [], "tts": []}

    ops = Operations(supervisor, config_file, engines=source, clock=lambda: now[0])
    await ops.dispatch("engines.list", {})
    await ops.dispatch("engines.list", {})
    assert len(calls) == 1
    now[0] = ops_module.CATALOGUE_TTL_S + 1
    await ops.dispatch("engines.list", {})
    assert len(calls) == 2


async def test_catalogue_failure_is_internal(supervisor: FakeSupervisor, config_file: Path) -> None:
    def broken() -> dict[str, object]:
        raise CatalogueUnavailable("engine catalogue took longer than 30s")

    ops = Operations(supervisor, config_file, engines=broken)
    with pytest.raises(OpError) as info:
        await ops.dispatch("engines.list", {})
    assert info.value.code == protocol.INTERNAL
    assert "longer than 30s" in info.value.message


def test_engines_in_subprocess() -> None:
    """The catalogue child process: the real one, and its failure modes."""
    result = engines_in_subprocess()
    assert "kokoro" in {e["name"] for e in result["tts"]}


def test_engines_in_subprocess_ignores_modules_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog's working directory is the user's home; a stray json.py
    there must not replace the standard library in the catalogue process."""
    (tmp_path / "json.py").write_text("raise SystemExit('shadowed')\n")
    monkeypatch.chdir(tmp_path)
    result = engines_in_subprocess()
    assert "kokoro" in {e["name"] for e in result["tts"]}


def test_engines_in_subprocess_runs_the_entry_points_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog's own interpreter may be in a deleted keg; the
    catalogue runs the interpreter next to what bin/voiced resolves to."""
    import subprocess

    env_bin = tmp_path / "env" / "bin"
    env_bin.mkdir(parents=True)
    (env_bin / "voiced").write_text("")
    entry = tmp_path / "state" / "bin" / "voiced"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(env_bin / "voiced")
    seen: list[str] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv[0])
        return subprocess.CompletedProcess(argv, 0, '{"stt": [], "tts": []}', "")

    monkeypatch.setattr(ops_module.subprocess, "run", run)
    assert engines_in_subprocess(entry=entry) == {"stt": [], "tts": []}
    assert seen == [str(env_bin / "python")]


def test_engines_in_subprocess_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(ops_module.subprocess, "run", timeout)
    with pytest.raises(CatalogueUnavailable, match="longer than"):
        engines_in_subprocess(timeout_s=1)

    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", "Traceback\nImportError: plugin broke\n")

    monkeypatch.setattr(ops_module.subprocess, "run", failed)
    with pytest.raises(CatalogueUnavailable, match="plugin broke"):
        engines_in_subprocess()


async def test_concurrent_sets_do_not_lose_writes(ops: Operations, config_file: Path) -> None:
    await asyncio.gather(
        ops.dispatch("settings.set", {"tts.speed": 1.5}),
        ops.dispatch("settings.set", {"stt.model": "a/b"}),
        ops.dispatch("settings.set", {"zeroconf": False}),
        ops.dispatch("settings.set", {"tts.voice": "am_adam"}),
    )
    data = tomllib.loads(read(config_file))
    assert data["tts"]["speed"] == 1.5 and data["tts"]["default_voice"] == "am_adam"
    assert data["stt"]["model"] == "a/b" and data["server"]["zeroconf"] is False


async def test_tts_speak_answers_the_wav_file(ops: Operations) -> None:
    asked: list[tuple[str, str]] = []

    async def speak(text: str, voice: str) -> bytes:
        asked.append((text, voice))
        return b"RIFF...."

    ops.speak = speak
    result = await ops.dispatch("tts.speak", {"text": "Hi, I'm Heart.", "voice": "af_heart"})
    assert result == {"format": "wav", "audio": base64.b64encode(b"RIFF....").decode()}
    assert asked == [("Hi, I'm Heart.", "af_heart")]


@pytest.mark.parametrize(
    "args",
    [
        {"voice": "af_heart"},
        {"text": " ", "voice": "af_heart"},
        {"text": "x" * (MAX_TEXT_CHARS + 1), "voice": "af_heart"},
        {"text": "Hi.", "voice": 3},
        {"text": "Hi."},
    ],
)
async def test_tts_speak_bad_arguments(ops: Operations, args: dict[str, object]) -> None:
    with pytest.raises(OpError) as info:
        await ops.dispatch("tts.speak", args)
    assert info.value.code == protocol.BAD_REQUEST


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SpeechUnavailable("the watchdog is not running"), protocol.SERVICE_UNAVAILABLE),
        (SpeechRefused("the line is longer than 10 s"), protocol.BAD_REQUEST),
    ],
)
async def test_tts_speak_errors(ops: Operations, error: Exception, code: str) -> None:
    async def speak(text: str, voice: str) -> bytes:
        raise error

    ops.speak = speak
    with pytest.raises(OpError) as info:
        await ops.dispatch("tts.speak", {"text": "Hi.", "voice": "af_heart"})
    assert (info.value.code, info.value.message) == (code, str(error))


async def test_without_a_watchdog_nothing_is_spoken(ops: Operations) -> None:
    with pytest.raises(OpError) as info:  # the default: the CLI fallback has no watchdog
        await ops.dispatch("tts.speak", {"text": "Hi.", "voice": "af_heart"})
    assert info.value.code == protocol.SERVICE_UNAVAILABLE
