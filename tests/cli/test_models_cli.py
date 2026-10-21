"""``voiced models list|download|delete`` against a scratch Hugging Face cache (ADR 0022)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import hf_cache_support as hf
from voiced import cli_models, models
from voiced.config import Settings
from voiced.launchd.progress import DOWNLOAD_PREFIX
from voiced.layout import install_lock
from voiced.models import lockfile
from voiced.models.catalogue import Engine

pytest.importorskip("huggingface_hub")

KOKORO = "mlx-community/Kokoro-82M-bf16"
P31 = "gradium/phonon-31m-en"
P40 = "gradium/phonon-40m-en"
VOICES = "gradium/phonon-voices"
OLD = "0" * 40


def cache(hub: Path, repo: str, revision: str | None = None, ref: str | None = None) -> None:
    hf.add_locked(hub, models.lock()[repo], revision, ref)


def by_repo(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for e in view["engines"] for m in (*e["models"], *e["shared"])}


# list ------------------------------------------------------------------------


def test_list_says_what_is_downloaded_in_use_and_old(settings: Settings, model_cache: Path) -> None:
    cache(model_cache, KOKORO, ref="main")
    cache(model_cache, P31)
    cache(model_cache, P31, revision=OLD)
    out: list[str] = []
    assert cli_models.cmd_list(settings, out.append, as_json=True) == 0
    view = json.loads(out[0])
    assert view["cache"] == str(model_cache)
    engines = {e["name"]: e for e in view["engines"]}
    assert engines["phonon"]["kind"] == "tts" and engines["phonon"]["installed"]
    assert engines["phonon"]["voices"][:2] == ["default", "Zoey"]
    assert engines["fake"]["models"] == []  # a plugin: listed, nothing to manage
    rows = by_repo(view)
    kokoro = rows[KOKORO]
    assert kokoro["in_use"] and kokoro["downloaded"] and kokoro["refs"] == ["main"]
    assert kokoro["download_bytes"] == models.lock()[KOKORO].bytes
    assert kokoro["on_disk_bytes"] > 0 and kokoro["license"] == "Apache-2.0"
    assert rows[P31]["downloaded"] and not rows[P31]["in_use"]
    assert rows[P31]["previous"] == []  # OLD is not a revision the lock names
    assert rows[VOICES]["shared"] and not rows[VOICES]["downloaded"]
    assert rows[P40]["gated"] and rows[P40]["url"] == f"https://huggingface.co/{P40}"
    assert view["total_bytes"] == kokoro["on_disk_bytes"] + rows[P31]["on_disk_bytes"]


def test_list_carries_the_ratings_notes_and_the_recommended_defaults(
    settings: Settings, model_cache: Path
) -> None:
    out: list[str] = []
    cli_models.cmd_list(settings, out.append, as_json=True)
    rows = by_repo(json.loads(out[0]))
    p31 = next(m for m in models.catalogued() if m.id == P31)
    assert (rows[P31]["quality"], rows[P31]["speed"], rows[P31]["note"]) == (
        p31.quality,
        p31.speed,
        p31.note,
    )
    assert rows[P40]["quality"] is None and rows[P40]["speed"] is None  # not measured
    assert rows[VOICES]["note"] == ""
    recommended = sorted(i for i, row in rows.items() if row["recommended"])
    assert recommended == sorted(m.id for m in models.default_models())
    assert KOKORO in recommended and P31 not in recommended
    out.clear()
    cli_models.cmd_list(settings, out.append, as_json=False)
    lines = out[0].splitlines()
    assert lines[0].split()[3:5] == ["QUALITY", "SPEED"]
    assert next(line for line in lines if P40 in line).split()[3:5] == ["-", "-"]
    assert next(line for line in lines if P31 in line).split()[3:5] == [
        f"{p31.quality}/5",
        f"{p31.speed}/5",
    ]


def test_the_documented_list_has_the_fields_the_command_prints(
    settings: Settings, model_cache: Path
) -> None:
    """docs/operations.md's example, which the app's fixture test copies,
    names every field of an engine and a model, and no other."""
    doc = (Path(__file__).parents[2] / "docs" / "operations.md").read_text()
    block = doc.split("`voiced models list --json`, which the menu bar app reads:", 1)[1]
    example = json.loads(block.split("```json", 1)[1].split("```", 1)[0])
    out: list[str] = []
    cli_models.cmd_list(settings, out.append, as_json=True)
    view = json.loads(out[0])
    assert example.keys() == view.keys()
    engine = next(e for e in view["engines"] if e["models"])
    assert example["engines"][0].keys() == engine.keys()
    assert example["engines"][0]["models"][0].keys() == engine["models"][0].keys()


def test_an_older_locked_revision_is_an_update(
    settings: Settings, model_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = models.lock()[P31]
    moved = models.Locked(P31, P31, "f" * 40, entry.patterns, entry.files, (entry.revision,))
    monkeypatch.setitem(models.lock(), P31, moved)
    cache(model_cache, P31, revision=entry.revision)  # at the old pin
    out: list[str] = []
    cli_models.cmd_list(settings, out.append, as_json=True)
    row = by_repo(json.loads(out[0]))[P31]
    assert not row["downloaded"] and row["previous"][0]["revision"] == entry.revision
    out.clear()
    cli_models.cmd_list(settings, out.append, as_json=False)
    assert any(
        P31 in line and "update available; old version" in line for line in out[0].splitlines()
    )
    assert out[-1] == "This folder is shared with other apps; voiced deletes only its own models."


# download --------------------------------------------------------------------


class FakeHub:
    """``snapshot`` as the download sees it: byte events, then the model in
    the scratch cache, or ``fail`` raised after the first byte event."""

    def __init__(self, hub: Path, fail: BaseException | None = None) -> None:
        self.hub, self.fail, self.got = hub, fail, []

    def __call__(self, model_id: str, *, write: Callable[[str], None] | None = None) -> Path:
        self.got.append(model_id)
        if write is not None:
            line = {"kind": "bytes", "repo": model_id, "bytes": 5, "total": 9}
            write(DOWNLOAD_PREFIX + json.dumps(line))
        if self.fail is not None:
            raise self.fail
        if model_id in models.lock():
            hf.add_locked(self.hub, models.lock()[model_id])
        return self.hub


@pytest.fixture
def fake_hub(monkeypatch: pytest.MonkeyPatch, model_cache: Path) -> FakeHub:
    fake = FakeHub(model_cache)
    monkeypatch.setattr("voiced.models.cache.snapshot", fake)
    monkeypatch.setattr("voiced.models.cache.cache_dir", lambda: str(model_cache))
    monkeypatch.setattr(cli_models.models, "cache_dir", lambda: str(model_cache))
    return fake


def events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_download_fetches_the_model_and_its_engines_shared_files(
    settings: Settings, fake_hub: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_models.cmd_download(P31, settings, print, json_progress=True) == 0
    assert fake_hub.got == [P31, VOICES]
    got = events(capsys)
    assert [e["event"] for e in got] == ["hello", "download", "download", "done"]
    assert got[1] == {"event": "download", "model": P31, "bytes": 5, "total": 9}
    assert models.not_downloaded(models.hub_dir(None), P31) == []


def test_a_repository_outside_the_catalogue_is_fetched_whole_with_a_notice(
    settings: Settings, fake_hub: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_models.cmd_download("someone/voice", settings, print, json_progress=False) == 0
    assert fake_hub.got == ["someone/voice"]
    assert "not in voiced's catalogue: downloading it whole at `main`" in capsys.readouterr().out


def test_download_is_busy_while_an_install_holds_the_lock(
    settings: Settings, fake_hub: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.install.venv_dir.mkdir(parents=True)
    with install_lock(settings.install.venv_dir):
        assert cli_models.cmd_download(P31, settings, print, json_progress=True) == 1
    assert fake_hub.got == []
    error = events(capsys)[-1]
    assert (error["event"], error["code"]) == ("error", "busy")
    assert error["message"] == cli_models.BUSY


def test_download_needs_the_bytes_and_a_gigabyte_free(
    settings: Settings, fake_hub: FakeHub, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    free = models.lock()[P31].bytes + models.lock()[VOICES].bytes + 10**9 - 1
    monkeypatch.setattr(
        "voiced.models.cache.shutil.disk_usage", lambda p: SimpleNamespace(free=free)
    )
    assert cli_models.cmd_download(P31, settings, print, json_progress=True) == 1
    assert fake_hub.got == [] and events(capsys)[-1]["code"] == "disk"


def test_a_shared_repository_is_not_downloaded_alone(
    settings: Settings, fake_hub: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_models.cmd_download(VOICES, settings, print, json_progress=True) == 1
    assert fake_hub.got == [] and events(capsys)[-1]["code"] == "failed"


def _http_error(cls: type[Exception], status: int) -> Exception:
    import httpx

    request = httpx.Request("GET", "https://huggingface.co/x")
    return cls(str(status), response=httpx.Response(status, request=request))


@pytest.mark.parametrize(
    ("fail", "code"),
    [
        (lambda: __import__("httpx").ReadError("connection reset"), "network"),
        (lambda: OSError(28, "No space left on device"), "disk"),
        (lambda: ValueError("odd"), "failed"),
    ],
    ids=["network", "disk", "other"],
)
def test_a_download_that_fails_after_its_first_bytes_ends_with_the_code(
    settings: Settings, fake_hub: FakeHub, capsys: pytest.CaptureFixture[str],
    fail: Callable[[], BaseException], code: str,
) -> None:  # fmt: skip
    fake_hub.fail = models.ModelsError(*models.classify(fail(), P31))
    assert cli_models.cmd_download(P31, settings, print, json_progress=True) == 1
    got = events(capsys)
    assert [e["event"] for e in got] == ["hello", "download", "error"]
    assert got[-1]["code"] == code


def test_snapshot_classifies_what_the_hub_raises(
    model_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    import huggingface_hub

    def fails(repo: str, **kwargs: Any) -> Any:
        raise httpx.ReadError("connection reset")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fails)
    monkeypatch.setattr("voiced.models.cache.cache_dir", lambda: str(model_cache))
    with pytest.raises(models.ModelsError) as exc:
        models.snapshot(P31)
    assert exc.value.code == "network"


def test_an_interrupted_download_still_ends_with_an_error(
    settings: Settings, fake_hub: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hub.fail = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        cli_models.cmd_download(P31, settings, print, json_progress=True)
    assert events(capsys)[-1] == {
        "event": "error",
        "step": None,
        "code": "failed",
        "message": "interrupted",
    }


def test_sigterm_ends_a_download_at_once(tmp_path: Path) -> None:
    """The app's Cancel: the event goes out and the process exits 143 while
    the download threads still have seconds to go (raising in the main
    thread would wait for them)."""
    import subprocess
    import sys
    import time

    script = tmp_path / "slow.py"
    script.write_text(
        "import time, sys\n"
        "from voiced import cli_models\n"
        "from voiced.config import Settings\n"
        "from concurrent.futures import ThreadPoolExecutor\n"
        "def slow(model_id, *, write=None):\n"
        "    # As hf_thread_map does: the main thread waits on download threads.\n"
        "    with ThreadPoolExecutor(2) as pool:\n"
        "        jobs = [pool.submit(time.sleep, 30) for _ in range(2)]\n"
        "        print('started', file=sys.stderr, flush=True)\n"
        "        for job in jobs:\n"
        "            job.result()\n"
        "import voiced.models.cache as cache\n"
        "cache.snapshot = slow\n"
        "cache.check_disk = lambda hub, needed: None\n"
        "s = Settings()\n"
        f"s.install.venv_dir = __import__('pathlib').Path({str(tmp_path / 'state')!r})\n"
        "code = cli_models.cmd_download('gradium/phonon-31m-en', s, print, json_progress=True)\n"
        "raise SystemExit(code)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert proc.stderr is not None
    while "started" not in proc.stderr.readline():
        pass
    started = time.monotonic()
    proc.terminate()
    out, _ = proc.communicate(timeout=10)
    assert time.monotonic() - started < 3
    assert proc.returncode == 143
    last = json.loads(out.splitlines()[-1])
    assert (last["event"], last["code"]) == ("error", "failed")
    assert last["message"] == cli_models.CANCELLED


@pytest.mark.parametrize(
    ("status", "token", "code"),
    [(401, None, "token_missing"), (401, "hf_x", "token_invalid"), (403, "hf_x", "gated_terms")],
)
def test_error_codes_for_a_gated_model(
    status: int, token: str | None, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import huggingface_hub
    from huggingface_hub.errors import GatedRepoError

    monkeypatch.setattr(huggingface_hub, "get_token", lambda: token)
    try:
        raise RuntimeError("wrapped") from _http_error(GatedRepoError, status)
    except RuntimeError as exc:
        assert models.classify(exc, P40)[0] == code


def test_a_repository_that_does_not_exist_is_not_a_token_problem() -> None:
    """The hub answers 401 for a name it does not know: not the token sheet."""
    from huggingface_hub.errors import RepositoryNotFoundError

    error = _http_error(RepositoryNotFoundError, 401)
    assert models.classify(error, "someone/typo") == (
        "failed",
        "someone/typo was not found on Hugging Face",
    )


def test_error_codes_for_the_network_and_anything_else() -> None:
    from huggingface_hub.errors import LocalEntryNotFoundError, OfflineModeIsEnabled

    assert models.classify(LocalEntryNotFoundError("offline"), P31)[0] == "network"
    assert models.classify(OfflineModeIsEnabled("offline"), P31)[0] == "network"
    assert models.classify(ConnectionResetError(), P31)[0] == "network"
    assert models.classify(OSError(28, "No space left on device"), P31)[0] == "disk"
    assert models.classify(ValueError("bad"), P31) == ("failed", "bad")
    # Only explicit causes count, not an exception swallowed on the way.
    try:
        try:
            raise LocalEntryNotFoundError("swallowed")
        except LocalEntryNotFoundError:
            raise ValueError("the real failure") from None
    except ValueError as exc:
        assert models.classify(exc, P31)[0] == "failed"


# delete ----------------------------------------------------------------------


def delete_args(repo: str, **kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(model=repo, previous=False, yes=True, json=True, **kwargs)


def test_delete_refuses_the_model_in_use_and_what_voiced_does_not_pin(
    settings: Settings, model_cache: Path
) -> None:
    cache(model_cache, KOKORO)
    for repo, code, says in [
        (KOKORO, "in_use", "is in use. Choose another model first."),
        ("someone/else", "failed", "voiced deletes only the models it pins"),
        (VOICES, "failed", "it is deleted with its engine's last model"),
    ]:
        out: list[str] = []
        assert cli_models.cmd_delete(delete_args(repo), settings, out.append) == 1
        error = json.loads(out[0])["error"]
        assert error["code"] == code and says in error["message"]
    assert models.not_downloaded(model_cache, KOKORO) == []


def test_delete_the_last_model_of_an_engine_takes_its_shared_files(
    settings: Settings, model_cache: Path
) -> None:
    cache(model_cache, P31, ref="main")
    cache(model_cache, P40)
    cache(model_cache, VOICES)
    out: list[str] = []
    assert cli_models.cmd_delete(delete_args(P31), settings, out.append) == 0
    result = json.loads(out[0])
    assert [d["repo"] for d in result["deleted"]] == [P31]  # 40M still needs the voices
    assert result["freed"] > 0
    assert result["notes"] == [cli_models.REF_NOTE.format(refs="`main`")]
    assert not (model_cache / models.repo_folder(P31)).exists()
    out.clear()
    assert cli_models.cmd_delete(delete_args(P40), settings, out.append) == 0
    result = json.loads(out[0])
    assert sorted(d["repo"] for d in result["deleted"]) == [P40, VOICES]
    assert result["notes"] == [f"Phonon's shared files ({VOICES}) are deleted with it."]


def test_delete_previous_keeps_the_pin_even_in_use(
    settings: Settings, model_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = models.lock()[KOKORO]
    monkeypatch.setitem(
        models.lock(),
        KOKORO,
        models.Locked(KOKORO, KOKORO, entry.revision, entry.patterns, entry.files, (OLD,)),
    )
    cache(model_cache, KOKORO)
    cache(model_cache, KOKORO, revision=OLD)
    out: list[str] = []
    args = delete_args(KOKORO)
    args.previous = True
    assert cli_models.cmd_delete(args, settings, out.append) == 0
    assert [d["revision"] for d in json.loads(out[0])["deleted"]] == [OLD]
    assert models.not_downloaded(model_cache, KOKORO) == []


def test_delete_asks_first_without_yes(settings: Settings, model_cache: Path) -> None:
    cache(model_cache, P31)
    args = argparse.Namespace(model=P31, previous=False, yes=False, json=False)
    out: list[str] = []
    assert cli_models.cmd_delete(args, settings, out.append, ask=lambda q: "n") == 1
    assert out[0].startswith(f"Delete {P31}? This frees ")
    assert out[0].endswith(
        f"Downloading it again takes {models.size_text(models.lock()[P31].bytes)}."
    )
    assert out[-1] == "nothing deleted"
    assert cli_models.cmd_delete(args, settings, out.append, ask=lambda q: "y") == 0
    assert out[-1].startswith(f"deleted {P31}, freed ")


def test_models_in_one_download_are_deleted_together_unless_one_is_in_use(
    settings: Settings, model_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sizes as folders of one repository: huggingface_hub deletes a
    revision whole, so deleting one takes the other, which is refused while
    that one is in use."""
    small = models.Model("org/assets", "MIT", ("small/*",), id="org/assets-small")
    base = models.Model("org/assets", "MIT", ("base/*",), id="org/assets-base")
    engine = Engine("assets", "stt", models=(small, base))
    catalogue = (*models.CATALOGUE, engine)
    monkeypatch.setattr(models, "CATALOGUE", catalogue)
    monkeypatch.setattr("voiced.models.lookup.CATALOGUE", catalogue)
    for m, name in ((small, "small/w"), (base, "base/w")):
        entry = models.Locked(
            m.id, m.repo, SHA, m.patterns, {name: lockfile.LockedFile(1, "e" * 64)}
        )
        monkeypatch.setitem(models.lock(), m.id, entry)
    hf.add_revision(model_cache, "org/assets", SHA, {"small/w": b"s", "base/w": b"b"})
    rows = by_repo(json.loads(_listed(settings)))
    assert rows["org/assets-small"]["downloaded"] and rows["org/assets-base"]["downloaded"]
    assert rows["org/assets-small"]["repo"] == "org/assets"

    settings.stt.model = base.id
    out: list[str] = []
    assert cli_models.cmd_delete(delete_args(small.id), settings, out.append) == 1
    assert json.loads(out[0])["error"] == {
        "code": "in_use",
        "message": f"org/assets-small shares org/assets at {SHA[:12]} with org/assets-base, "
        "which is in use; voiced deletes a revision whole. Choose another model first.",
    }
    settings.stt.model = "mlx-community/parakeet-tdt-0.6b-v3"
    out.clear()
    assert cli_models.cmd_delete(delete_args(small.id), settings, out.append) == 0
    result = json.loads(out[0])
    assert result["notes"] == ["org/assets-base is in the same download and deleted with it."]
    rows = by_repo(json.loads(_listed(settings)))
    assert not rows["org/assets-small"]["downloaded"] and not rows["org/assets-base"]["downloaded"]


def test_delete_reports_any_failure_as_json(
    settings: Settings, model_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache(model_cache, P31)

    def broken(hub: Path) -> Any:
        raise OSError("the cache cannot be read")

    monkeypatch.setattr("huggingface_hub.scan_cache_dir", broken)
    out: list[str] = []
    assert cli_models.cmd_delete(delete_args(P31), settings, out.append) == 1
    assert json.loads(out[0]) == {
        "error": {"code": "failed", "message": "the cache cannot be read"}
    }


SHA = "a" * 40


def _listed(settings: Settings) -> str:
    out: list[str] = []
    cli_models.cmd_list(settings, out.append, as_json=True)
    return out[0]
