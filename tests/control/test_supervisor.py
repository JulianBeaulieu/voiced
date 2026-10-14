"""The watchdog as the control channel's supervisor."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from voiced.cli_lifecycle import watchdog_specs
from voiced.config import Settings, WatchdogConfig
from voiced.control.ops import NotInstalled
from voiced.control.supervisor import WatchdogSupervisor, service_state
from voiced.watchdog import Observation, RestartResult, ServiceSpec, Watchdog

POLICY = WatchdogConfig(startup_grace_s=100)
HEALTH = {
    "status": "ok",
    "engine": "kokoro",
    "model": "m",
    "model_loaded": True,
    "uptime_s": 12.5,
    "queue_depth": 0,
}


@pytest.mark.parametrize(
    ("obs", "grace", "state"),
    [
        (Observation("ok"), False, "running"),
        (Observation("starting"), False, "starting"),
        (Observation("degraded"), False, "degraded"),
        (Observation("stalled"), False, "degraded"),
        (Observation("error"), False, "degraded"),
        (Observation("unreachable", loaded=False), True, "stopped"),
        (Observation("unreachable", loaded=None), False, "unknown"),
        (Observation("unreachable", loaded=True), True, "starting"),
        (Observation("unreachable", loaded=True), False, "degraded"),
    ],
)
def test_service_state(obs: Observation, grace: bool, state: str) -> None:
    assert service_state(obs, grace) == state


def make(settings: Settings, restarted: list[str], clock: list[float]) -> WatchdogSupervisor:
    def restart(spec: ServiceSpec) -> RestartResult:
        restarted.append(spec.name)
        return RestartResult(spec.name != "stt", f"did {spec.name}")

    dog = Watchdog(
        services=watchdog_specs(settings),
        policy=POLICY,
        restart=restart,
        observe=lambda s, p: Observation("ok", payload=HEALTH),
        clock=lambda: clock[0],
    )
    views = {
        "stt": Observation("unreachable", loaded=True, pid=7),
        "tts": Observation("ok", payload=HEALTH, loaded=True, pid=8),
    }
    return WatchdogSupervisor(
        dog,
        settings,
        specs_for=watchdog_specs,
        installed=lambda label: True,
        observe=lambda spec: views[spec.name],
    )


def test_status_shape(settings: Settings) -> None:
    clock = [1000.0]
    sup = make(settings, [], clock)
    sup.dog.tick()  # the grace period starts now
    status = sup.status()
    assert status["installed"] is True
    stt, tts, wd = (status["services"][k] for k in ("stt", "tts", "watchdog"))
    assert stt == {
        "state": "starting",
        "pid": 7,
        "health": None,
        "engine": "fake",
        "model": settings.stt.model,
        "warm": False,
        "uptime_s": None,
        "restarts": 0,
    }
    assert tts["state"] == "running" and tts["engine"] == "kokoro" and tts["warm"] is True
    assert tts["uptime_s"] == 12.5 and tts["health"] == HEALTH
    assert wd["state"] == "running" and wd["pid"] == os.getpid()
    clock[0] += 200  # grace over
    assert sup.status()["services"]["stt"]["state"] == "degraded"


def test_restart_goes_through_the_watchdog(settings: Settings) -> None:
    restarted: list[str] = []
    clock = [1000.0]
    sup = make(settings, restarted, clock)
    assert sup.restart("tts") == "did tts"
    state = sup.dog.states["tts"]
    assert state.restarts == 1 and state.last_restart_at == 1000.0
    assert sup.status()["services"]["tts"]["restarts"] == 1
    with pytest.raises(NotInstalled):
        sup.restart("stt")  # RestartResult(done=False)
    assert restarted == ["tts", "stt"]


def test_settings_changed_updates_the_specs(settings: Settings) -> None:
    sup = make(settings, [], [0.0])
    changed = settings.model_copy(deep=True)
    changed.server.api_key = "new"
    sup.settings_changed(changed)
    assert all(spec.api_key == "new" for spec in sup.dog.services)


async def test_run_async_stops() -> None:
    ticks: list[int] = []
    three = threading.Event()

    def observe(spec: ServiceSpec, policy: WatchdogConfig) -> Observation:
        ticks.append(1)
        if len(ticks) >= 3:
            three.set()
        return Observation("ok")

    dog = Watchdog(
        services=[ServiceSpec("stt", "x", "")],
        policy=WatchdogConfig(interval_s=0.01),
        observe=observe,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(dog.run_async(stop))
    assert await asyncio.to_thread(three.wait, 2)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert dog.observations["stt"].kind == "ok"
