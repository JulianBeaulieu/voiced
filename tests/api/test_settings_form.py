"""The status page's optional settings form: off by default, key and LAN rules."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from voiced.api.status_page import FORM_LAN_WITHOUT_KEY, form_refusal
from voiced.config import Settings, config_path
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
    assert doc["settings_form"] == {"enabled": False, "refused": None, "api_key_required": False}
    r = await tts_client.get("/settings.json")
    assert r.status_code == 404
    assert "browser_settings" in r.json()["error"]["message"]
    r = await tts_client.post("/settings", json={"tts.speed": 2})
    assert r.status_code == 404


async def test_on_loopback_without_key(tts_client: AsyncClient) -> None:
    path = write_config(browser_settings=True)
    doc = (await tts_client.get("/metrics.json")).json()
    assert doc["settings_form"] == {"enabled": True, "refused": None, "api_key_required": False}
    got = (await tts_client.get("/settings.json")).json()
    assert got["settings"]["tts"]["engine"] == "fake"
    assert got["settings"]["paths"]["config"] == str(path)
    assert {e["name"] for e in got["engines"]["tts"]} >= {"fake", "kokoro"}
    r = await tts_client.post("/settings", json={"tts.voice": "bf_emma", "tts.speed": 1.5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restart_required"] == ["tts"]
    assert body["tts"] == {"engine": "fake", "model": body["tts"]["model"], "voice": "bf_emma",
                           "speed": 1.5}  # fmt: skip
    assert tomllib.loads(path.read_text())["tts"]["default_voice"] == "bf_emma"


async def test_invalid_value_is_400_and_not_written(tts_client: AsyncClient) -> None:
    path = write_config(browser_settings=True)
    before = path.read_text()
    r = await tts_client.post("/settings", json={"tts.speed": -1})
    assert r.status_code == 400
    assert r.json()["error"] == {
        "message": "tts.speed: must be greater than 0 and at most 4",
        "type": "invalid_setting",
    }
    assert path.read_text() == before


async def test_api_key_required_when_set(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True, api_key="k1")
    doc = (await tts_client.get("/metrics.json")).json()
    assert doc["settings_form"]["api_key_required"] is True
    assert (await tts_client.get("/settings.json")).status_code == 401
    assert (await tts_client.get("/settings.json", headers=bearer("nope"))).status_code == 401
    got = await tts_client.get("/settings.json", headers=bearer("k1"))
    assert got.status_code == 200
    assert "k1" not in got.text
    assert (await tts_client.post("/settings", json={"zeroconf": False})).status_code == 401
    r = await tts_client.post("/settings", json={"zeroconf": False}, headers=bearer("k1"))
    assert r.status_code == 200


async def test_lan_without_key_is_refused(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True, bind="0.0.0.0")
    doc = (await tts_client.get("/metrics.json")).json()
    assert doc["settings_form"]["refused"] == FORM_LAN_WITHOUT_KEY
    r = await tts_client.get("/settings.json")
    assert r.status_code == 403
    assert r.json()["error"]["message"] == FORM_LAN_WITHOUT_KEY
    assert (await tts_client.post("/settings", json={"api_key": "x"})).status_code == 403


async def test_lan_with_key_is_allowed(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True, bind="0.0.0.0", api_key="k1")
    assert (await tts_client.get("/settings.json", headers=bearer("k1"))).status_code == 200


async def test_running_lan_counts_even_if_file_says_loopback(
    tts_app: FastAPI, settings: Settings
) -> None:
    settings.server.bind = "0.0.0.0"  # what this process was started with
    write_config(browser_settings=True)  # the file: loopback, no key
    async with lifespan_client(tts_app) as c:
        assert (await c.get("/settings.json")).status_code == 403


async def test_cross_origin_and_content_type(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True)
    r = await tts_client.post(
        "/settings", json={"zeroconf": False}, headers={"Origin": "http://elsewhere.example"}
    )
    assert r.status_code == 403
    r = await tts_client.post(
        "/settings", content=b'{"zeroconf": false}', headers={"Content-Type": "text/plain"}
    )
    assert r.status_code == 415
    r = await tts_client.post("/settings", json=["zeroconf"])
    assert r.status_code == 400


async def test_turning_the_form_off_takes_effect_at_once(tts_client: AsyncClient) -> None:
    write_config(browser_settings=True)
    r = await tts_client.post("/settings", json={"browser_settings": False})
    assert r.status_code == 200
    assert r.json()["restart_required"] == []
    assert (await tts_client.get("/settings.json")).status_code == 404


@pytest.mark.parametrize(
    ("live", "running", "auth", "expected"),
    [
        ({"browser_settings": False}, {}, "", 404),
        ({"browser_settings": True}, {}, "", None),
        ({"browser_settings": True, "bind": "0.0.0.0"}, {}, "", 403),
        ({"browser_settings": True}, {"bind": "0.0.0.0"}, "", 403),
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
