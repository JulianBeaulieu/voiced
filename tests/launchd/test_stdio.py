"""The daemons open their own stdout/stderr logs, never following a symlink."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from voiced.launchd import stdio
from voiced.launchd.plist import ENV_STDIO_LOG


def _child_env(prefix: Path) -> dict[str, str]:
    return {**os.environ, ENV_STDIO_LOG: str(prefix)}


def test_open_log_creates_0600_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "svc.stderr.log"
    path.write_bytes(b"old\n")
    path.chmod(0o600)
    fd = stdio.open_log(path)
    try:
        flags = os.get_blocking(fd), os.get_inheritable(fd)
        os.write(fd, b"new\n")
    finally:
        os.close(fd)
    assert path.read_bytes() == b"old\nnew\n"
    assert flags == (True, False)  # O_NONBLOCK cleared, O_CLOEXEC kept

    fresh = tmp_path / "fresh.log"
    os.close(stdio.open_log(fresh))
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_open_log_uses_o_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real_open = os.open

    def spy(path: Path, flags: int, mode: int = 0o777) -> int:
        seen.append(flags)
        return real_open(path, flags, mode)

    monkeypatch.setattr(stdio.os, "open", spy)
    os.close(stdio.open_log(tmp_path / "x.log"))
    want = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    assert seen and seen[0] & want == want


def test_open_log_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "root-only"
    target.write_bytes(b"precious\n")
    link = tmp_path / "svc.stderr.log"
    link.symlink_to(target)
    with pytest.raises(OSError) as info:
        stdio.open_log(link)
    assert info.value.errno == errno.ELOOP
    assert target.read_bytes() == b"precious\n"


def test_open_log_refuses_a_dangling_symlink(tmp_path: Path) -> None:
    target = tmp_path / "sudoers.d" / "x"
    link = tmp_path / "svc.stdout.log"
    link.symlink_to(target)
    with pytest.raises(OSError):
        stdio.open_log(link)
    assert not target.exists()


def test_open_log_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "svc.stdout.log"
    os.mkfifo(fifo)
    with pytest.raises(OSError):  # ENXIO: no reader; never blocks
        stdio.open_log(fifo)


def test_no_env_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_STDIO_LOG, raising=False)
    calls: list[object] = []
    monkeypatch.setattr(stdio, "redirect_stdio", lambda *a: calls.append(a))
    stdio.redirect_stdio_from_env()
    assert calls == []


def test_env_is_consumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_STDIO_LOG, str(tmp_path / "stt"))
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(stdio, "redirect_stdio", lambda out, err: calls.append((out, err)))
    stdio.redirect_stdio_from_env()
    stdio.redirect_stdio_from_env()  # unset now: second call does nothing
    assert calls == [(tmp_path / "stt.stdout.log", tmp_path / "stt.stderr.log")]
    assert ENV_STDIO_LOG not in os.environ


CHILD = """
import ctypes, os, sys
from voiced.launchd.stdio import redirect_stdio_from_env
redirect_stdio_from_env()
print("python stdout")
libc = ctypes.CDLL(None)
libc.write(2, b"c-level stderr\\n", 15)
libc.write(1, b"c-level stdout\\n", 15)
print("env", os.environ.get("VOICED_STDIO_LOG"))
raise SystemExit("python stderr")
"""


def test_child_c_level_output_lands_in_the_files(tmp_path: Path) -> None:
    logs = tmp_path / "logs"  # created on demand
    result = subprocess.run(
        [sys.executable, "-c", CHILD],
        env=_child_env(logs / "tts"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == b"" and result.stderr == b""
    out = (logs / "tts.stdout.log").read_text()
    err = (logs / "tts.stderr.log").read_text()
    assert "python stdout" in out and "c-level stdout" in out
    assert "env None" in out  # not passed on to children
    assert "c-level stderr" in err and "python stderr" in err


def test_child_keeps_its_stderr_when_the_log_is_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "root-only"
    target.write_bytes(b"precious\n")
    (tmp_path / "tts.stderr.log").symlink_to(target)
    result = subprocess.run(
        [sys.executable, "-c", CHILD],
        env=_child_env(tmp_path / "tts"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert target.read_bytes() == b"precious\n"
    assert b"not redirecting stdout/stderr" in result.stderr
    assert b"c-level stderr" in result.stderr  # still on the original stderr
    assert not (tmp_path / "tts.stdout.log").read_bytes()  # opened, never used


def test_console_script_redirects_before_the_cli(tmp_path: Path) -> None:
    """``voiced.entry`` redirects first: even argparse's error lands in the file."""
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from voiced.entry import main; sys.exit(main())", "x"],
        env=_child_env(tmp_path / "stt"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr == b""
    assert "invalid choice" in (tmp_path / "stt.stderr.log").read_text()
