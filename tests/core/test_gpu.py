"""The GPU lock that puts STT encoder calls ahead of TTS synthesis (ADR 0019)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from voiced import gpu


@pytest.fixture
def lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the module at a lock file of the test's own, with fresh descriptors."""
    path = tmp_path / gpu.LOCK_NAME
    monkeypatch.setattr(gpu, "lock_path", lambda: path)
    monkeypatch.setattr(gpu, "_fds", {})
    monkeypatch.setattr(gpu, "_warned", False)
    yield path
    for fd in gpu._fds.values():
        os.close(fd)


def test_lock_path_ignores_tmpdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # launchd jobs start without TMPDIR; a terminal has it. Both must agree.
    if sys.platform != "darwin":
        pytest.skip("the per-user temporary directory is a macOS confstr")
    before = gpu.lock_path()
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert gpu.lock_path() == before
    assert before.name == gpu.LOCK_NAME


def test_stt_turn_is_seen_by_the_probe_in_the_same_process(lock_file: Path) -> None:
    assert not gpu.stt_running()
    with gpu.stt_turn():
        assert gpu.stt_running()
        assert gpu.stt_running()  # probing does not take or weaken the lock
    assert not gpu.stt_running()
    assert lock_file.exists()


def test_another_process_holding_the_lock_and_its_exit(lock_file: Path) -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import fcntl, os, sys, time
                fd = os.open({str(lock_file)!r}, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                print("held", flush=True)
                time.sleep(60)
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        assert gpu.stt_running()
        # The STT never waits for it: the call runs without the lock.
        ran = threading.Event()

        def call() -> None:
            with gpu.stt_turn():
                ran.set()

        thread = threading.Thread(target=call, daemon=True)
        started = time.perf_counter()
        thread.start()
        thread.join(timeout=5)
        assert ran.is_set()
        assert time.perf_counter() - started < 1.0
        holder.kill()  # a crashed STT: the kernel drops its lock
        holder.wait(timeout=10)
        assert not gpu.stt_running()
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_yielder_waits_for_the_running_call(lock_file: Path) -> None:
    taken = threading.Event()

    def call() -> None:
        with gpu.stt_turn():
            taken.set()
            time.sleep(0.1)

    thread = threading.Thread(target=call)
    thread.start()
    assert taken.wait(5)
    yielder = gpu.Yielder(budget_s=5.0)
    started = time.perf_counter()
    yielder()
    waited = time.perf_counter() - started
    thread.join()
    assert not gpu.stt_running()
    assert 0.05 < waited < 2.0
    assert yielder.waited_s == pytest.approx(waited, abs=0.01)


def test_yielder_does_not_wait_when_free(lock_file: Path) -> None:
    yielder = gpu.Yielder()
    yielder()
    assert yielder.waited_s == 0.0


def test_yielder_budget_covers_the_whole_sentence(lock_file: Path) -> None:
    with gpu.stt_turn():
        yielder = gpu.Yielder(budget_s=0.05)
        started = time.perf_counter()
        for _ in range(20):
            yielder()
        elapsed = time.perf_counter() - started
    assert 0.05 <= yielder.waited_s < 0.5
    assert elapsed < 0.5  # not 20 budgets


def test_unopenable_lock_file_turns_the_gate_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(gpu, "lock_path", lambda: tmp_path / "missing" / gpu.LOCK_NAME)
    monkeypatch.setattr(gpu, "_fds", {})
    monkeypatch.setattr(gpu, "_warned", False)
    with caplog.at_level(logging.WARNING, logger="voiced.gpu"):
        with gpu.stt_turn():
            assert not gpu.stt_running()
        gpu.Yielder()()
    warnings = [r for r in caplog.records if "GPU priority" in r.getMessage()]
    assert len(warnings) == 1


def test_lock_file_symlink_is_not_followed(lock_file: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    lock_file.symlink_to(target)
    with gpu.stt_turn():
        assert not gpu.stt_running()
    assert target.read_text() == "keep"


def test_lock_file_of_another_user_is_not_used(
    lock_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lock_file.touch()
    real = os.getuid()
    monkeypatch.setattr(gpu.os, "getuid", lambda: real + 1)
    with caplog.at_level(logging.WARNING, logger="voiced.gpu"):
        with gpu.stt_turn():
            assert not gpu.stt_running()
    assert gpu._fds == {}
    assert any("another user" in r.getMessage() for r in caplog.records)
