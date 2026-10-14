"""The watchdog as the control channel's supervisor."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Sequence
from dataclasses import replace

import pytest

from voiced.cli_lifecycle import watchdog_specs
from voiced.config import Settings, WatchdogConfig
from voiced.control.ops import NotInstalled, ServiceUnavailable
from voiced.control.supervisor import WatchdogSupervisor, service_state
from voiced.launchd.control import CommandResult
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
    with pytest.raises(ServiceUnavailable):
        sup.restart("stt")  # RestartResult(done=False)
    assert restarted == ["tts", "stt"]


def test_check_restart(settings: Settings) -> None:
    printed: list[int] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(printed[-1] if printed else 0, "")

    dog = Watchdog(services=watchdog_specs(settings), policy=POLICY)
    plists = {"stt": True, "tts": False}
    sup = WatchdogSupervisor(
        dog, runner=runner, installed=lambda label: plists[label.rsplit(".", 1)[1]]
    )
    sup.check_restart("stt")  # plist and loaded
    with pytest.raises(NotInstalled, match="no launchd plist"):
        sup.check_restart("tts")
    printed.append(113)  # launchctl print: not found
    with pytest.raises(ServiceUnavailable, match="not loaded"):
        sup.check_restart("stt")


def test_engine_and_model_are_what_the_service_was_started_with(settings: Settings) -> None:
    """Without /health, status shows the spec's engine and model, which only
    change when the watchdog restarts the service or sees a new pid."""
    file_engine = ["fake"]

    def respec(spec: ServiceSpec) -> ServiceSpec:
        return replace(spec, engine=file_engine[0], api_key="new")

    pid = [7]
    dog = Watchdog(
        services=watchdog_specs(settings),
        policy=POLICY,
        restart=lambda spec: RestartResult(True, "ok"),
        observe=lambda s, p: Observation("unreachable", loaded=True, pid=pid[0]),
        respec=respec,
    )
    sup = WatchdogSupervisor(
        dog,
        installed=lambda label: True,
        observe=lambda spec: Observation("unreachable", loaded=True, pid=pid[0]),
    )
    dog.tick()
    file_engine[0] = "soprano"  # the file changes; the process does not
    dog.tick()
    assert sup.status()["services"]["tts"]["engine"] == "fake"
    assert dog.spec("tts").api_key == settings.server.api_key  # type: ignore[union-attr]
    sup.restart("tts")
    assert sup.status()["services"]["tts"]["engine"] == "soprano"
    assert dog.spec("tts").api_key == "new"  # type: ignore[union-attr]
    file_engine[0] = "kokoro"
    pid[0] = 9  # launchd restarted stt by itself
    dog.tick()
    assert sup.status()["services"]["stt"]["engine"] == "kokoro"


def test_restart_does_not_wait_for_a_health_poll(settings: Settings) -> None:
    """A tick observes without the lock, so a requested restart goes through
    while a poll hangs."""
    in_poll, release = threading.Event(), threading.Event()

    def observe(spec: ServiceSpec, policy: WatchdogConfig) -> Observation:
        in_poll.set()
        release.wait(5)
        return Observation("ok")

    dog = Watchdog(
        services=watchdog_specs(settings),
        policy=POLICY,
        observe=observe,
        restart=lambda spec: RestartResult(True, "ok"),
    )
    ticker = threading.Thread(target=dog.tick)
    ticker.start()
    assert in_poll.wait(5)
    result = [RestartResult(False, "")]
    restarter = threading.Thread(target=lambda: result.__setitem__(0, dog.restart_now("tts")))
    restarter.start()
    restarter.join(5)
    hung = restarter.is_alive()
    release.set()
    ticker.join(5)
    assert not hung
    assert result[0].done


async def test_run_async_stops() -> None:
    ticked = threading.Event()

    def observe(spec: ServiceSpec, policy: WatchdogConfig) -> Observation:
        ticked.set()
        return Observation("ok")

    dog = Watchdog(
        services=[ServiceSpec("stt", "x", "")],
        policy=WatchdogConfig(interval_s=3600),
        observe=observe,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(dog.run_async(stop))
    assert await asyncio.to_thread(ticked.wait, 5)
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_run_async_survives_observer_bug(caplog: pytest.LogCaptureFixture) -> None:
    ticked = threading.Event()

    def boom(spec: ServiceSpec, policy: WatchdogConfig) -> Observation:
        ticked.set()
        raise RuntimeError("bug")

    dog = Watchdog(
        services=[ServiceSpec("stt", "x", "")],
        policy=WatchdogConfig(interval_s=3600),
        observe=boom,
    )
    stop = asyncio.Event()
    with caplog.at_level("ERROR"):
        task = asyncio.create_task(dog.run_async(stop))
        assert await asyncio.to_thread(ticked.wait, 5)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert "tick failed" in caplog.text
