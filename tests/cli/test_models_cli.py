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

pytest.importorskip("huggingface_hub")

KOKORO = "mlx-community/Kokoro-82M-bf16"
P31 = "gradium/phonon-31m-en"
P40 = "gradium/phonon-40m-en"
VOICES = "gradium/phonon-voices"
OLD = "0" * 40


def cache(hub: Path, repo: str, revision: str | None = None, ref: str | None = None) -> None:
    entry = models.lock()[repo]
    files = {name: f"{repo}/{name}/{revision}".encode() for name in entry.files}
    hf.add_revision(hub, repo, revision or entry.revision, files, ref=ref)


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


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch, model_cache: Path) -> list[str]:
    """``snapshot`` downloads into the scratch cache, reporting its bytes."""
    got: list[str] = []

    def snapshot(repo: str, *, write: Callable[[str], None] | None = None) -> Path:
        got.append(repo)
        if write is not None:
            write(
                DOWNLOAD_PREFIX
                + json.dumps({"kind": "bytes", "repo": repo, "bytes": 5, "total": 9})
            )
        if repo in models.lock():
            cache(model_cache, repo)
        return model_cache

    monkeypatch.setattr(models, "snapshot", snapshot)
    return got


def events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_download_fetches_the_model_and_its_engines_shared_files(
    settings: Settings, fetched: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_models.cmd_download(P31, settings, print, json_progress=True) == 0
    assert fetched == [P31, VOICES]
    got = events(capsys)
    assert [e["event"] for e in got] == ["hello", "download", "download", "done"]
    assert got[1] == {"event": "download", "model": P31, "bytes": 5, "total": 9}
    assert models.not_downloaded(models.hub_dir(None), P31) == []


def test_a_repository_outside_the_catalogue_is_fetched_whole_with_a_notice(
    settings: Settings, fetched: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_models.cmd_download("someone/voice", settings, print, json_progress=False) == 0
    assert fetched == ["someone/voice"]
    assert "not in voiced's catalogue: downloading it whole at `main`" in capsys.readouterr().out


def test_download_is_busy_while_an_install_holds_the_lock(
    settings: Settings, fetched: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    settings.install.venv_dir.mkdir(parents=True)
    with install_lock(settings.install.venv_dir):
        assert cli_models.cmd_download(P31, settings, print, json_progress=True) == 1
    assert fetched == []
    error = events(capsys)[-1]
    assert (error["event"], error["code"]) == ("error", "busy")
    assert error["message"] == cli_models.BUSY


def test_download_needs_the_bytes_and_a_gigabyte_free(
    settings: Settings, fetched: list[str], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    free = models.lock()[P31].bytes + models.lock()[VOICES].bytes + 10**9 - 1
    monkeypatch.setattr(cli_models.shutil, "disk_usage", lambda p: SimpleNamespace(free=free))
    assert cli_models.cmd_download(P31, settings, print, json_progress=True) == 1
    assert fetched == [] and events(capsys)[-1]["code"] == "disk"


def test_a_shared_repository_is_not_downloaded_alone(
    settings: Settings, fetched: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_models.cmd_download(VOICES, settings, print, json_progress=True) == 1
    assert fetched == [] and events(capsys)[-1]["code"] == "failed"


def _http_error(cls: type[Exception], status: int) -> Exception:
    import httpx

    request = httpx.Request("GET", "https://huggingface.co/x")
    return cls(str(status), response=httpx.Response(status, request=request))


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
        assert cli_models.error_code(exc) == code


def test_error_codes_for_the_network_and_anything_else() -> None:
    from huggingface_hub.errors import LocalEntryNotFoundError

    assert cli_models.error_code(LocalEntryNotFoundError("offline")) == "network"
    assert cli_models.error_code(ConnectionResetError()) == "network"
    assert cli_models.error_code(OSError(28, "No space left on device")) == "disk"
    assert cli_models.error_code(ValueError("bad")) == "failed"


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
    assert result["notes"] == [f"Phonon's shared files ({VOICES}) go with it."]


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


def test_a_revision_another_downloaded_model_shares_is_not_deleted(
    settings: Settings, model_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sizes as folders of one repository: huggingface_hub deletes a
    revision whole, so deleting one while the other is downloaded is refused."""
    small = models.Model("org/assets", "MIT", ("small/*",), id="org/assets-small")
    base = models.Model("org/assets", "MIT", ("base/*",), id="org/assets-base")
    engine = models.Engine("assets", "stt", models=(small, base))
    monkeypatch.setattr(models, "CATALOGUE", (*models.CATALOGUE, engine))
    for m, name in ((small, "small/w"), (base, "base/w")):
        entry = models.Locked(m.id, m.repo, SHA, m.patterns, {name: models.LockedFile(1, "e" * 64)})
        monkeypatch.setitem(models.lock(), m.id, entry)
    hf.add_revision(model_cache, "org/assets", SHA, {"small/w": b"s", "base/w": b"b"})
    out: list[str] = []
    assert cli_models.cmd_delete(delete_args(small.id), settings, out.append) == 1
    assert json.loads(out[0])["error"]["message"] == (
        f"org/assets-small shares org/assets at {SHA[:12]} with org/assets-base, which is "
        "downloaded; voiced deletes a revision whole, so delete org/assets-base too or keep both"
    )
    rows = by_repo(json.loads(_listed(settings)))
    assert rows["org/assets-small"]["downloaded"] and rows["org/assets-base"]["downloaded"]
    assert rows["org/assets-small"]["repo"] == "org/assets"


SHA = "a" * 40


def _listed(settings: Settings) -> str:
    out: list[str] = []
    cli_models.cmd_list(settings, out.append, as_json=True)
    return out[0]
