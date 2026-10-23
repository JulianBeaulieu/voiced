"""``scripts/app_manifest.py`` writes the menu bar app's install-manifest.json."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.scripts_support import load

UV_LOCK = """\
version = 1

[[package]]
name = "numpy"
version = "2.5.4"
sdist = { url = "numpy-2.5.4.tar.gz", hash = "sha256:00", size = 9000 }
wheels = [
    { url = "numpy-2.5.4-cp312-cp312-macosx_14_0_arm64.whl", hash = "sha256:01", size = 300 },
    { url = "numpy-2.5.4-cp312-cp312-macosx_11_0_arm64.whl", hash = "sha256:02", size = 200 },
    { url = "numpy-2.5.4-cp312-cp312-macosx_15_0_arm64.whl", hash = "sha256:03", size = 999 },
    { url = "numpy-2.5.4-cp313-cp313-macosx_14_0_arm64.whl", hash = "sha256:04", size = 999 },
    { url = "numpy-2.5.4-cp312-cp312-macosx_14_0_x86_64.whl", hash = "sha256:05", size = 999 },
]

[[package]]
name = "numpy"
version = "2.4.6"
wheels = [{ url = "numpy-2.4.6-cp311-cp311-macosx_14_0_arm64.whl", hash = "sha256:06", size = 999 }]

[[package]]
name = "anyio"
version = "4.15.1"
wheels = [{ url = "anyio-4.15.1-py3-none-any.whl", hash = "sha256:07", size = 40 }]

[[package]]
name = "docopt"
version = "0.6.2"
sdist = { url = "docopt-0.6.2.tar.gz", hash = "sha256:08", size = 5 }
"""

LOCK = """\
anyio==4.15.1 \\
    --hash=sha256:07
docopt==0.6.2 \\
    --hash=sha256:08
numpy==2.4.6 ; python_full_version < '3.12' \\
    --hash=sha256:06
numpy==2.5.4 ; python_full_version >= '3.12' \\
    --hash=sha256:01
colorama==0.4.6 ; sys_platform == 'win32' \\
    --hash=sha256:09
"""

CATALOGUE = {
    "engines": [
        {
            "name": name,
            "label": name.capitalize(),
            "kind": kind,
            "models": [{"id": f"{name}-model", "recommended": True}],
            "shared": [],
        }
        for name, kind in (("parakeet", "stt"), ("kokoro", "tts"))
    ]
}
PINS = {
    "models": [
        {"repo": "mlx-community/parakeet-tdt-0.6b-v3", "revision": "e" * 40, "bytes": 2508532829},
    ],
    "catalogue": CATALOGUE,
}


@pytest.fixture(scope="module")
def app_manifest() -> ModuleType:
    return load("app_manifest")


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    found = {
        "uv_lock": tmp_path / "uv.lock",
        "lock": tmp_path / "requirements.lock",
        "pins": tmp_path / "model-pins.json",
        "wheel": tmp_path / "voiced-0.2.0rc1-py3-none-any.whl",
    }
    found["uv_lock"].write_text(UV_LOCK)
    found["lock"].write_text(LOCK)
    found["pins"].write_text(json.dumps(PINS))
    found["wheel"].write_bytes(b"a wheel")
    return found


def test_the_manifest(app_manifest: ModuleType, files: dict[str, Path]) -> None:
    manifest = app_manifest.manifest(files["wheel"], files["lock"], files["uv_lock"], files["pins"])
    assert manifest == {
        "version": "0.2.0rc1",
        "python": load("lock_target").python_version(),
        "lock_sha256": hashlib.sha256(files["lock"].read_bytes()).hexdigest(),
        "wheel": {
            "name": "voiced-0.2.0rc1-py3-none-any.whl",
            "sha256": hashlib.sha256(b"a wheel").hexdigest(),
        },
        # numpy's most specific wheel for macOS 14 arm64 and CPython 3.12, anyio's pure
        # wheel, docopt's sdist; nothing for the 3.11 numpy or Windows' colorama.
        "wheels_bytes": 300 + 40 + 5,
        "models": PINS["models"],
        "catalogue": CATALOGUE,
    }


@pytest.mark.parametrize("recommended", [[], ["a", "b"]])
def test_each_service_needs_one_recommended_model(
    app_manifest: ModuleType, files: dict[str, Path], recommended: list[str]
) -> None:
    """The setup preselects it; none, or two, would leave the choice unclear."""
    engine = {"name": "x", "kind": "tts", "shared": []}
    engine["models"] = [{"id": m, "recommended": True} for m in recommended]
    stt = CATALOGUE["engines"][0]
    files["pins"].write_text(json.dumps({**PINS, "catalogue": {"engines": [stt, engine]}}))
    with pytest.raises(SystemExit, match="tts needs one recommended model"):
        app_manifest.manifest(files["wheel"], files["lock"], files["uv_lock"], files["pins"])


def test_the_real_lock_has_a_file_for_every_entry(app_manifest: ModuleType) -> None:
    root = Path(__file__).resolve().parent.parent
    lock_target = load("lock_target")
    size = app_manifest.wheels_bytes(
        lock_target.LOCK, root / "uv.lock", lock_target.python_version()
    )
    assert size > 50_000_000


def test_a_package_only_newer_macos_installs_is_counted_once(
    app_manifest: ModuleType, tmp_path: Path
) -> None:
    """moonshine-voice installs on macOS 15 and later: its macOS 15 wheel is
    counted on top of the floor's download."""
    uv_lock = tmp_path / "uv.lock"
    uv_lock.write_text(
        UV_LOCK
        + """
[[package]]
name = "moonshine-voice"
version = "0.1.5"
wheels = [
    { url = "moonshine_voice-0.1.5-py3-none-macosx_15_0_arm64.whl", hash = "sha256:10", size = 7 },
]
"""
    )
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        LOCK + "moonshine-voice==0.1.5 ; platform_release >= '24' \\\n    --hash=sha256:10\n"
    )
    python = load("lock_target").python_version()
    assert app_manifest.wheels_bytes(lock, uv_lock, python) == 300 + 40 + 5 + 7


def test_a_lock_entry_missing_from_uv_lock(
    app_manifest: ModuleType, files: dict[str, Path]
) -> None:
    files["lock"].write_text(LOCK + "attrs==26.1.0 \\\n    --hash=sha256:10\n")
    with pytest.raises(
        SystemExit, match=re.escape("attrs==26.1.0 is in the lock but not in uv.lock")
    ):
        app_manifest.manifest(files["wheel"], files["lock"], files["uv_lock"], files["pins"])


def test_a_package_with_no_file_for_the_mac(
    app_manifest: ModuleType, files: dict[str, Path]
) -> None:
    files["uv_lock"].write_text(UV_LOCK.replace('sdist = { url = "docopt', "# "))
    with pytest.raises(SystemExit, match=re.escape("docopt 0.6.2: no file for this Mac")):
        app_manifest.manifest(files["wheel"], files["lock"], files["uv_lock"], files["pins"])


@pytest.mark.parametrize(
    ("model", "message"),
    [
        ({"repo": "r", "revision": "main", "bytes": 1}, "the revision must be a commit SHA"),
        ({"repo": "r", "revision": "e" * 40}, "no size"),
        ({"repo": "r", "revision": "e" * 40, "bytes": 0}, "no size"),
        ({"repo": "r", "revision": "e" * 40, "bytes": True}, "no size"),
    ],
)
def test_bad_model_pins(
    app_manifest: ModuleType, files: dict[str, Path], model: dict[str, object], message: str
) -> None:
    files["pins"].write_text(json.dumps({"models": [model]}))
    with pytest.raises(SystemExit, match=message):
        app_manifest.manifest(files["wheel"], files["lock"], files["uv_lock"], files["pins"])


def test_no_models(app_manifest: ModuleType, files: dict[str, Path]) -> None:
    files["pins"].write_text('{"models": []}')
    with pytest.raises(SystemExit, match="no models"):
        app_manifest.manifest(files["wheel"], files["lock"], files["uv_lock"], files["pins"])


def test_not_a_voiced_wheel(app_manifest: ModuleType, files: dict[str, Path]) -> None:
    other = files["wheel"].with_name("other-1.0-py3-none-any.whl")
    other.write_bytes(b"x")
    with pytest.raises(SystemExit, match="is not a voiced wheel"):
        app_manifest.manifest(other, files["lock"], files["uv_lock"], files["pins"])


def test_the_command_line(
    app_manifest: ModuleType, files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--wheel", str(files["wheel"]), "--lock", str(files["lock"])]
    args += ["--models", str(files["pins"]), "--uv-lock", str(files["uv_lock"])]
    assert app_manifest.main(args) == 0
    assert json.loads(capsys.readouterr().out)["wheels_bytes"] == 345


def test_the_pins_bundle_sh_reads_are_voiced_models(
    app_manifest: ModuleType, tmp_path: Path
) -> None:
    """bundle.sh writes the pins with ``python -I -m voiced.models`` from the
    installed wheel; the manifest takes exactly what it prints."""
    from voiced import models
    from voiced.models import lockfile

    out = subprocess.run(
        [sys.executable, "-I", "-m", "voiced.models"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    pins = tmp_path / "model-pins.json"
    pins.write_text(out.stdout)
    assert app_manifest.models(pins) == lockfile.manifest()["models"]
    assert app_manifest.catalogue(pins) == lockfile.catalogue_view()
    assert [(m["repo"], m["revision"], m["bytes"]) for m in app_manifest.models(pins)] == [
        (p.repo, p.revision, p.bytes)
        for p in (models.lock()[models.default_model(e).repo] for e in ("parakeet", "kokoro"))
    ]
