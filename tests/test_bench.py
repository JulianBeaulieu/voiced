"""``scripts/bench.py`` sends the API key a server with ``server.api_key`` requires."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

BENCH = Path(__file__).resolve().parent.parent / "scripts" / "bench.py"


@pytest.fixture(scope="module")
def bench() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("bench", BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench"] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    yield module
    del sys.modules["bench"]


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, message: Any) -> None:
        self.sent.append(message)

    def recv(self, timeout: float) -> str:
        return json.dumps({"type": "final", "text": "hello"})


def handshake_headers(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch, api_key: str
) -> dict[str, str]:
    seen: dict[str, Any] = {}

    @contextmanager
    def connect(uri: str, **kwargs: Any) -> Iterator[FakeSocket]:
        seen.update(kwargs)
        yield FakeSocket()

    monkeypatch.setattr(bench.ws_sync, "connect", connect)
    text, _ = bench.stream_and_finalize("ws://host/stream", api_key, b"\0\0" * 160, 16_000, 160)
    assert text == "hello"
    headers: dict[str, str] = seen["additional_headers"]
    return headers


def test_websocket_handshake_carries_the_key(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = handshake_headers(bench, monkeypatch, "s3cret")
    assert headers == {"Authorization": "Bearer s3cret"}


def test_websocket_handshake_without_a_key_sends_no_authorization(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert handshake_headers(bench, monkeypatch, "") == {}


def test_openai_client_carries_the_key(bench: ModuleType) -> None:
    client = bench._openai("http://host:8880", "s3cret")
    assert client.auth_headers == {"Authorization": "Bearer s3cret"}
    assert str(client.base_url).rstrip("/") == "http://host:8880/v1"


def test_api_key_defaults_to_the_environment(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_API_KEY", "from-env")
    seen: list[str] = []

    def stop(url: str, api_key: str, voice: str, trials: int) -> list[float]:
        seen.append(api_key)
        raise SystemExit(0)

    monkeypatch.setattr(bench, "engine_name", lambda url: "fake")
    monkeypatch.setattr(bench, "tts_first_audio_ms", stop)
    with pytest.raises(SystemExit):
        bench.main(["--stt-url", "http://s", "--tts-url", "http://t"])
    assert seen == ["from-env"]


def test_table_names_the_auth_mode_but_never_the_key(
    bench: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.print_table([bench.Row("m", "v")], authenticated=True)
    assert "auth: key" in capsys.readouterr().out
    bench.print_table([bench.Row("m", "v")], authenticated=False)
    assert "auth: none" in capsys.readouterr().out
