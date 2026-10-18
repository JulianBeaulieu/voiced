"""The state directory's rules (ADR 0017): ownership, the entry point, the
install lock, and how the watchdog tells its environment was replaced."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from voiced.launchd.install import new_environment_dir
from voiced.layout import (
    ENV_ID,
    Manifest,
    NotVoiceds,
    RunningEnvironment,
    built_environments,
    claim,
    foreign_entries,
    homebrew_busy,
    homebrew_lock,
    install_lock,
    is_legacy_venv,
    legacy_venv_paths,
    link_entry_point,
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


def _uv_python(path: Path) -> Path:
    """A Python install directory as `uv python install` leaves it."""
    installation = path / "cpython-3.12.15-macos-aarch64-none"
    (installation / "bin").mkdir(parents=True)
    (installation / "bin" / "python3").write_text("")
    (path / ".temp").mkdir()
    (path / ".lock").write_text("")
    (path / ".gitignore").write_text("*")
    return path


def _app_build(state: Path) -> Path:
    """What the app has built when it runs `voiced install` (ADR 0018)."""
    _uv_python(state / "python")
    _venv(state / "envs" / "20261018T120000")
    (state / "control.sock").write_text("")
    return state


def test_claim_accepts_what_the_app_builds_before_install(tmp_path: Path) -> None:
    state = _app_build(tmp_path / "state")
    assert claim(state) is None  # voiced's, nothing installed yet
    (state / "envs" / "20261018T130000-2.partial").mkdir()  # a second build, unfinished
    python = state / "python"
    (python / "cpython-3.12-macos-aarch64-none").symlink_to(
        python / "cpython-3.12.15-macos-aarch64-none"
    )
    (python / ".cache").mkdir()
    assert claim(state) is None
    only_python = _uv_python(tmp_path / "step1" / "python").parent  # the venv step failed
    assert claim(only_python) is None


def _user_venv(state: Path) -> None:
    _venv(state / "envs" / "20261018T130000", voiced=False)


def _checkout_venv(state: Path) -> None:
    venv = _venv(state / "envs" / "20261018T130000")
    info = next(venv.glob("lib/python3*/site-packages/voiced-*.dist-info"))
    (info / "direct_url.json").write_text('{"url": "file:///x", "dir_info": {"editable": true}}')


def _symlink_into_envs(state: Path) -> None:
    _venv(state.parent / "elsewhere")
    (state / "envs" / "20261018T130000").symlink_to(state.parent / "elsewhere")


def _symlinked(name: str) -> Callable[[Path], None]:
    def make(state: Path) -> None:
        shutil.move(state / name, state.parent / name)
        (state / name).symlink_to(state.parent / name)

    return make


def _python_link_outside(state: Path) -> None:
    other = _uv_python(state.parent / "other")
    (state / "python" / "cpython-3.12-macos-aarch64-none").symlink_to(
        other / "cpython-3.12.15-macos-aarch64-none"
    )


def _empty_envs(state: Path) -> None:
    shutil.rmtree(state / "envs" / "20261018T120000")


def _unmarked_python(state: Path) -> None:
    (state / "python" / ".gitignore").unlink()


@pytest.mark.parametrize(
    ("make", "named"),
    [
        (lambda s: (s / "envs" / "notes.txt").write_text("mine"), "envs/notes.txt"),
        (_user_venv, "envs/20261018T130000"),
        (_checkout_venv, "envs/20261018T130000"),
        (lambda s: _venv(s / "envs" / "myenv"), "envs/myenv"),
        (
            lambda s: (s / "envs" / "20261018T130000.partial").write_text(""),
            "envs/20261018T130000.partial",
        ),
        (_symlink_into_envs, "envs/20261018T130000"),
        (lambda s: (s / "python" / "notes.txt").write_text("mine"), "python/notes.txt"),
        (lambda s: (s / "envs" / "control.sock").write_text(""), "envs/control.sock"),
        (lambda s: (s / "python" / "pypy-3.10").mkdir(), "python/pypy-3.10"),
        (
            lambda s: (s / "python" / "cpython-3.13.1-macos-aarch64-none").mkdir(),
            "python/cpython-3.13.1",
        ),
        (_python_link_outside, "python/cpython-3.12-macos"),
        (_unmarked_python, "python (not uv's)"),
        (lambda s: (s / "python" / ".gitignore").write_text("*.pyc"), "python (not uv's)"),
        (_symlinked("envs"), "envs"),
        (_symlinked("python"), "python"),
        (_empty_envs, "envs (empty)"),
        (lambda s: (s / "precious.txt").write_text("mine"), "precious.txt"),
        (lambda s: (s / "bin").mkdir(), "bin"),
    ],
    ids=[
        "foreign-file-in-envs",
        "user-venv-without-voiced",
        "editable-checkout-venv",
        "env-not-named-by-voiced",
        "partial-that-is-a-file",
        "symlink-in-envs",
        "foreign-file-in-python",
        "socket-in-envs",
        "foreign-dir-in-python",
        "python-installation-without-interpreter",
        "python-link-outside",
        "python-without-uv-lock-marker",
        "python-with-another-gitignore",
        "symlinked-envs",
        "symlinked-python",
        "empty-envs",
        "foreign-file-beside-them",
        "bin-without-manifest",
    ],
)
def test_claim_refuses_anything_else_without_a_manifest(
    tmp_path: Path, make: Callable[[Path], object], named: str
) -> None:
    """Uninstall would delete what claim accepts here, and once deleted a
    user's venv adopted by mistake: any entry voiced did not make refuses
    the whole directory."""
    state = _app_build(tmp_path / "state")
    make(state)
    with pytest.raises(NotVoiceds, match=re.escape(named)):
        claim(state)
    assert any(entry.startswith(named) for entry in foreign_entries(state))


def test_an_empty_python_dir_is_not_uvs(tmp_path: Path) -> None:
    (tmp_path / "state" / "python").mkdir(parents=True)
    with pytest.raises(NotVoiceds, match=re.escape("python (not uv's)")):
        claim(tmp_path / "state")


def test_new_environments_are_named_as_claim_expects(tmp_path: Path) -> None:
    first = new_environment_dir(tmp_path)
    _venv(first)
    second = new_environment_dir(tmp_path)
    assert ENV_ID.fullmatch(first.name) and ENV_ID.fullmatch(second.name)


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
