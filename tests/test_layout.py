"""The state directory's rules (ADR 0017): ownership, the entry point, the
install lock, and how the watchdog tells its environment was replaced."""

from __future__ import annotations

import fcntl
import os
import shutil
import threading
from pathlib import Path

import pytest

from voiced.layout import (
    Manifest,
    NotVoiceds,
    RunningEnvironment,
    SymlinkedStateDir,
    built_environments,
    claim,
    homebrew_busy,
    homebrew_lock,
    install_lock,
    is_legacy_venv,
    legacy_venv_paths,
    link_entry_point,
    partial_environments,
    read_link,
    write_manifest,
)


def _venv(path: Path, *, voiced: bool = True) -> Path:
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "python").write_text("")
    (path / "bin" / "voiced").write_text("")
    site = path / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    if voiced:
        (site / "voiced-0.0.1.dist-info").mkdir()
    (path / "pyvenv.cfg").write_text("home = /x\n")
    return path


# Ownership -------------------------------------------------------------------


def test_a_venv_without_voiced_is_not_a_legacy_install(tmp_path: Path) -> None:
    """A user's own venv given as the state dir is refused, not adopted
    (adopting it would delete its lib and bin on the first switch)."""
    mine = _venv(tmp_path / "mine", voiced=False)
    assert not is_legacy_venv(mine)
    with pytest.raises(NotVoiceds, match=r"pyvenv\.cfg"):
        claim(mine)
    legacy = _venv(tmp_path / "legacy")
    assert is_legacy_venv(legacy)
    adopted = claim(legacy)
    assert adopted is not None and adopted.environment == legacy


def test_an_editable_checkout_venv_is_not_a_legacy_install(tmp_path: Path) -> None:
    """`uv sync` installs voiced editable; `voiced install` never did."""
    venv = _venv(tmp_path / ".venv")
    info = next(venv.glob("lib/python3*/site-packages/voiced-*.dist-info"))
    (info / "direct_url.json").write_text('{"url": "file:///x", "dir_info": {"editable": true}}')
    assert not is_legacy_venv(venv)
    (info / "direct_url.json").write_text('{"url": "file:///x", "dir_info": {}}')
    assert is_legacy_venv(venv)


def test_claim_is_one_rule(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert claim(state) is None  # missing
    state.mkdir()
    (state / "control.sock").write_text("")
    assert claim(state) is None  # a watchdog ran here before any install
    (state / "precious.txt").write_text("mine")
    with pytest.raises(NotVoiceds, match=r"precious\.txt"):
        claim(state)
    write_manifest(state, Manifest(source="app", environment=None))
    claimed = claim(state)
    assert claimed is not None and claimed.source == "app"


@pytest.mark.parametrize("contents", ["empty", "manifest", "legacy"])
def test_claim_refuses_a_symlinked_state_dir(tmp_path: Path, contents: str) -> None:
    """Uninstall would delete inside whatever the link points at."""
    target = tmp_path / "target"
    if contents == "legacy":
        _venv(target)
    else:
        target.mkdir()
    if contents == "manifest":
        write_manifest(target, Manifest(source="app", environment=None))
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SymlinkedStateDir) as refused:
        claim(link)
    assert refused.value.real == target.resolve()
    assert f"--state-dir {target.resolve()}" in str(refused.value)
    assert claim(target) is None or contents != "empty"


def test_partial_environments_are_real_directories_only(tmp_path: Path) -> None:
    envs = tmp_path / "envs"
    unfinished = envs / "20261018T120000.partial"
    unfinished.mkdir(parents=True)
    _venv(envs / "20261018T110000")
    (tmp_path / "elsewhere").mkdir()
    (envs / "20261018T130000.partial").symlink_to(tmp_path / "elsewhere")
    (envs / "20261018T140000.partial").write_text("")
    assert partial_environments(tmp_path) == [unfinished]


def test_legacy_venv_paths_never_include_the_entry_point(tmp_path: Path) -> None:
    legacy = _venv(tmp_path / "legacy")
    (legacy / "include").mkdir()
    (legacy / "notes.txt").write_text("mine")
    names = {p.relative_to(legacy).as_posix() for p in legacy_venv_paths(legacy)}
    assert names == {"lib", "include", "pyvenv.cfg", "bin/python"}


def test_built_environments_are_complete_venvs_only(tmp_path: Path) -> None:
    """A directory without pyvenv.cfg, and a build in progress, are never
    pruned or uninstalled as environments."""
    envs = tmp_path / "envs"
    done = _venv(envs / "20260101T000000")
    _venv(envs / "20260102T000000.partial")
    (envs / "someones-dir").mkdir()
    assert built_environments(tmp_path) == [done]


# The entry point ---------------------------------------------------------------


def test_link_entry_point_never_removes_the_old_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repointing is one rename: a daemon starting meanwhile finds either
    link, never none."""
    state = tmp_path / "state"
    link_entry_point(state, tmp_path / "a")
    entry = state / "bin" / "voiced"
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        assert self != entry, "the entry point was removed before the new one was in place"
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    link_entry_point(state, tmp_path / "b")
    assert os.readlink(entry) == str(tmp_path / "b" / "bin" / "voiced")


def test_link_entry_point_refuses_itself_through_a_symlinked_state_dir(tmp_path: Path) -> None:
    """A legacy venv reached as ~/.voiced -> /elsewhere: linking its
    bin/voiced to itself would be a loop."""
    real = _venv(tmp_path / "real")
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(ValueError, match="itself"):
        link_entry_point(alias, real)
    assert (real / "bin" / "voiced").is_file() and not (real / "bin" / "voiced").is_symlink()


# The install lock ----------------------------------------------------------------


def test_install_lock_serialises_installs(tmp_path: Path) -> None:
    order: list[str] = []
    waited = threading.Event()
    with install_lock(tmp_path):

        def second() -> None:
            with install_lock(tmp_path, waited.set):
                order.append("second")

        thread = threading.Thread(target=second)
        thread.start()
        assert waited.wait(5)
        order.append("first")
    thread.join(5)
    assert order == ["first", "second"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [], "no lock file is left"


# The running environment -------------------------------------------------------


def test_running_environment_notices_a_replaced_environment(tmp_path: Path) -> None:
    """A deleted keg (brew cleanup), a deleted interpreter (python@3.12
    upgrade) or a repointed entry point (voiced install) all count."""
    env_a, env_b = _venv(tmp_path / "a"), _venv(tmp_path / "b")
    entry = tmp_path / "state" / "bin" / "voiced"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(env_a / "bin" / "voiced")
    running = RunningEnvironment.of((env_a,), entry)
    assert running.replaced() is None
    entry.unlink()
    entry.symlink_to(env_b / "bin" / "voiced")
    assert "now points at" in (running.replaced() or "")
    entry.unlink()
    entry.symlink_to(env_a / "bin" / "voiced")
    shutil.rmtree(env_a)
    assert "no longer exists" in (running.replaced() or "")


def test_running_environment_notices_a_keg_rebuilt_in_place(tmp_path: Path) -> None:
    """``brew reinstall`` renames the keg to ``<keg>.reinstall`` and builds a
    new one at the same path: same path, new inode."""
    keg = _venv(tmp_path / "Cellar" / "voiced" / "1" / "libexec")
    running = RunningEnvironment.of((keg,), tmp_path / "voiced")
    keg.rename(keg.with_name("libexec.reinstall"))
    _venv(keg)
    assert "was replaced" in (running.replaced() or "")


def test_running_environment_waits_for_homebrew_to_delete_the_old_keg(tmp_path: Path) -> None:
    """``brew upgrade`` repoints ``opt/voiced`` before it has finished the
    new keg; only the old keg's deletion, after that, counts."""
    old_keg = _venv(tmp_path / "Cellar" / "voiced" / "1" / "libexec")
    new_keg = _venv(tmp_path / "Cellar" / "voiced" / "2" / "libexec")
    opt = tmp_path / "opt" / "voiced"
    opt.parent.mkdir()
    opt.symlink_to(old_keg.parent)
    entry = tmp_path / "state" / "bin" / "voiced"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(opt / "libexec" / "bin" / "voiced")
    running = RunningEnvironment.of((old_keg,), entry)
    opt.unlink()
    opt.symlink_to(new_keg.parent)
    assert running.replaced() is None
    shutil.rmtree(old_keg.parent)
    assert "no longer exists" in (running.replaced() or "")


def test_running_environment_notices_a_python_upgrade_behind_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sys.base_prefix`` is ``opt/python@3.12/...``: a patch upgrade
    repoints it and cleanup deletes the keg it pointed at."""
    old_keg, new_keg = tmp_path / "python" / "3.12.1", tmp_path / "python" / "3.12.2"
    for keg in (old_keg, new_keg):
        keg.mkdir(parents=True)
    opt = tmp_path / "opt-python"
    opt.symlink_to(old_keg)
    monkeypatch.setattr("sys.base_prefix", str(opt))
    running = RunningEnvironment.current(tmp_path / "no entry point")
    opt.unlink()
    opt.symlink_to(new_keg)
    assert running.replaced() is None
    old_keg.rmdir()
    assert str(old_keg.resolve().parent) in (running.replaced() or "")


def test_running_environment_notices_a_legacy_venv_replaced_by_a_link(tmp_path: Path) -> None:
    entry = tmp_path / "bin" / "voiced"
    entry.parent.mkdir()
    entry.write_text("#!/bin/sh\n")
    running = RunningEnvironment.of((), entry)
    assert running.entry_link is None and read_link(entry) is None
    assert running.replaced() is None
    entry.unlink()
    entry.symlink_to(tmp_path / "envs" / "x" / "bin" / "voiced")
    assert "now points at" in (running.replaced() or "")


def test_homebrew_busy_while_the_formula_lock_is_held(tmp_path: Path) -> None:
    keg_file = tmp_path / "Cellar" / "python@3.12" / "3.12.15" / "Frameworks"
    lock = homebrew_lock(keg_file)
    assert lock == tmp_path / "var" / "homebrew" / "locks" / "python@3.12.formula.lock"
    assert homebrew_lock(tmp_path / "envs" / "x") is None
    assert homebrew_busy((keg_file,)) is None  # no lock file: not installing
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    assert homebrew_busy((keg_file,)) is None
    with lock.open() as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # as Homebrew's LockFile does
        assert homebrew_busy((keg_file,)) == "python@3.12"
    assert homebrew_busy((keg_file,)) is None
