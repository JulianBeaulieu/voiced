"""The status page's optional settings form: off by default, always behind the API key."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from voiced.api.status_page import FORM_NEEDS_KEY, form_refusal
from voiced.config import Settings, config_path
from voiced.engines.soprano import SopranoEngine
from voiced.settings_store import LiveSettings
from voiced.testing import lifespan_client


def write_config(**server: object) -> Path:
    """The live config file (``$VOICED_CONFIG``) with the fake engines."""
    lines = ["[server]"]
    for key, value in server.items():
        rendered = str(value).lower() if isinstance(value, bool) else f'"{value}"'
        lines.append(f"{key} = {rendered}")
    lines += ["[stt]", 'engine = "fake"', "[tts]", 'engine = "fake"']
    path = config_path()
    path.write_text("\n".join(lines) + "\n")
    return path


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_off_by_default(tts_client: AsyncClient) -> None:
    doc = (await tts_client.get("/metrics.json")).json()
    assert doc["settings_form"] == {"enabled": False, "refused": None}
    r = await tts_client.get("/settings.json")
    assert r.status_code == 404
    assert "browser_settings" in r.json()["error"]["message"]
    r = await tts_client.post("/settings", json={"tts.speed": 2})
    assert r.status_code == 404


async def test_without_a_key_it_is_refused_on_loopback_too(tts_client: AsyncClient) -> None:
    """A DNS-rebinding page in the operator's browser reaches loopback too, so
    with no key the form is refused whatever ``bind`` is."""
    path = write_config(browser_settings=True)
    before = path.read_text()
    doc = (await tts_client.get("/metrics.json")).json()
    assert doc["settings_form"] == {
        "enabled": True,
        "refused": FORM_NEEDS_KEY,
    }
    r = await tts_client.get("/settings.json")
    assert r.status_code == 403
    assert r.json()["error"]["message"] == FORM_NEEDS_KEY
    rebinding = {"Host": "evil.example:8880", "Origin": "http://evil.example:8880"}
    r = await tts_client.post("/settings", json={"tts.model": "attacker/repo"}, headers=rebinding)
    assert r.status_code == 403
    assert path.read_text() == before


async def test_change_with_the_key(tts_client: AsyncClient) -> None:
    path = write_config(browser_settings=True, api_key="k1")
    got = (await tts_client.get("/settings.json", headers=bearer("k1"))).json()
    assert got["settings"]["tts"]["engine"] == "fake"
    assert got["settings"]["paths"]["config"] == str(path)
    fake = next(e for e in got["engines"]["tts"] if e["name"] == "fake")
    assert fake["label"] == "Fake"
    assert fake["voice_labels"]["af_heart"] == "Heart (American English, female)"
    r = await tts_client.post(
        "/settings", json={"tts.voice": "bf_emma", "tts.speed": 1.5}, headers=bearer("k1")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restart_required"] == ["tts"]
    assert body["changed"] == ["tts.voice", "tts.speed"]
    assert body["tts"] == {"enabled": True, "engine": "fake", "model": body["tts"]["model"],
                           "voice": "bf_emma", "speed": 1.5, "idle_unload_s": 0}  # fmt: skip
    assert tomllib.loads(path.read_text())["tts"]["default_voice"] == "bf_emma"


async def test_a_model_that_is_not_downloaded_is_409(tts_client: AsyncClient) -> None:
    path = write_config(browser_settings=True, api_key="k1")
    before = path.read_text()
    change = {"tts.engine": "phonon", "tts.model": "gradium/phonon-40m-en"}
    r = await tts_client.post("/settings", json=change, headers=bearer("k1"))
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "model_not_downloaded"
    assert path.read_text() == before


async def test_invalid_value_is_400_and_not_written(tts_client: AsyncClient) -> None:
    path = write_config(browser_settings=True, api_key="k1")
    before = path.read_text()
    r = await tts_client.post("/settings", json={"tts.speed": -1}, headers=bearer("k1"))
    assert r.status_code == 400
    assert r.json()["error"] == {
        "message": "tts.speed: must be at least 0.25",
        "type": "invalid_setting",
    }
    assert path.read_text() == before


@pytest.mark.usefixtures("models_downloaded")
async def test_engine_change_resets_the_model(tts_client: AsyncClient) -> None:
    path = write_config(browser_settings=True, api_key="k1")
    r = await tts_client.post("/settings", json={"tts.engine": "soprano"}, headers=bearer("k1"))
    assert r.status_code == 200, r.text
    tts = tomllib.loads(path.read_text())["tts"]
    assert tts["engine"] == "soprano"
    assert tts["model"] == SopranoEngine.model_id
    assert tts["default_voice"] == "default"


async def test_api_key_required_when_set(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True, api_key="k1")
    doc = (await tts_client.get("/metrics.json")).json()
    assert doc["settings_form"] == {"enabled": True, "refused": None}
    assert (await tts_client.get("/settings.json")).status_code == 401
    assert (await tts_client.get("/settings.json", headers=bearer("nope"))).status_code == 401
    got = await tts_client.get("/settings.json", headers=bearer("k1"))
    assert got.status_code == 200
    assert "k1" not in got.text
    assert (await tts_client.post("/settings", json={"zeroconf": False})).status_code == 401
    r = await tts_client.post("/settings", json={"zeroconf": False}, headers=bearer("k1"))
    assert r.status_code == 200


async def test_lan_with_key_is_allowed(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True, bind="0.0.0.0", api_key="k1")
    assert (await tts_client.get("/settings.json", headers=bearer("k1"))).status_code == 200


async def test_running_key_counts_while_the_file_has_none(
    tts_app: FastAPI, settings: Settings
) -> None:
    settings.server.api_key = "old"  # what this process was started with
    write_config(browser_settings=True)  # the file: no key
    async with lifespan_client(tts_app) as c:
        assert (await c.get("/settings.json")).status_code == 401
        assert (await c.get("/settings.json", headers=bearer("old"))).status_code == 200


async def test_content_type_and_body(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True, api_key="k1")
    r = await tts_client.post(
        "/settings",
        content=b'{"zeroconf": false}',
        headers={"Content-Type": "text/plain", **bearer("k1")},
    )
    assert r.status_code == 415
    r = await tts_client.post("/settings", json=["zeroconf"], headers=bearer("k1"))
    assert r.status_code == 400


async def test_turning_the_form_off_takes_effect_at_once(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True, api_key="k1")
    r = await tts_client.post("/settings", json={"browser_settings": False}, headers=bearer("k1"))
    assert r.status_code == 200
    assert r.json()["restart_required"] == []
    assert (await tts_client.get("/settings.json", headers=bearer("k1"))).status_code == 404


@pytest.mark.parametrize(
    ("live", "running", "auth", "expected"),
    [
        ({"browser_settings": False}, {}, "", 404),
        ({"browser_settings": True}, {}, "", 403),
        ({"browser_settings": True}, {}, "Bearer ", 403),
        ({"browser_settings": True, "bind": "0.0.0.0"}, {}, "", 403),
        ({"browser_settings": True, "api_key": "a"}, {}, "", 401),
        ({"browser_settings": True, "api_key": "a"}, {}, "Bearer a", None),
        ({"browser_settings": True}, {"api_key": "old"}, "Bearer old", None),
        ({"browser_settings": True}, {"api_key": "old"}, "", 401),
        ({"browser_settings": True, "api_key": "new"}, {"api_key": "old"}, "Bearer old", None),
        ({"browser_settings": True, "api_key": "a"}, {}, "Basic a", 401),
    ],
)
def test_form_refusal(
    settings: Settings,  # clears VOICED_* from the environment
    live: dict[str, object],
    running: dict[str, object],
    auth: str,
    expected: int | None,
) -> None:
    refusal = form_refusal(
        Settings.model_validate({"server": running}),
        Settings.model_validate({"server": live}),
        auth,
    )
    assert (refusal.status if refusal else None) == expected


async def test_page_has_the_form(tts_client: AsyncClient) -> None:
    html = (await tts_client.get("/")).text
    assert 'id="settings-form"' in html
    assert "/settings.json" in html


def test_live_settings_are_reread_only_when_the_file_changes(settings: Settings) -> None:
    write_config(browser_settings=True)
    live = LiveSettings()
    first = live.get()
    assert first is not None and first.server.browser_settings
    assert live.get() is first  # unchanged file: not parsed again
    write_config(browser_settings=False, api_key="longer-so-the-size-changes")
    second = live.get()
    assert second is not None and not second.server.browser_settings
    config_path().write_text("[server\n")
    assert live.get() is None  # logged, shown as "form off"
    assert live.last is second  # what a service falls back to meanwhile


def test_an_unreadable_file_is_parsed_and_logged_once_per_change(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    config_path().write_text("[server\n")
    live = LiveSettings()
    for _ in range(3):  # a service asks on every request
        assert live.get() is None
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    config_path().write_text("[tts\n")  # a different broken file: logged again
    assert live.get() is None
    assert len(caplog.records) == 2
