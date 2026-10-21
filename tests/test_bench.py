"""``scripts/bench.py`` sends the API key a server with ``server.api_key`` requires."""

from __future__ import annotations

import argparse
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
    client = bench.openai_client("http://host:8880", "s3cret")
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


def test_first_audio_while_another_client_streams(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row measures first audio while a second client streams to the STT
    the whole time, and stops that client afterwards (ADR 0019)."""
    import threading

    import numpy as np

    streamed: list[str] = []
    measured: list[str] = []
    talking = threading.Event()

    def stream_and_finalize(ws_url: str, api_key: str, *rest: Any) -> tuple[str, float]:
        talking.set()
        streamed.append(ws_url)
        threading.Event().wait(0.001)
        return "text", 1.0

    def first_audio_ms(client: Any, voice: str, text: str) -> float:
        assert talking.is_set() or not measured  # the warm-up may come first
        measured.append(voice)
        return 50.0

    monkeypatch.setattr(bench, "stream_and_finalize", stream_and_finalize)
    monkeypatch.setattr(bench, "first_audio_ms", first_audio_ms)
    monkeypatch.setattr(bench, "openai_client", lambda url, key: object())
    monkeypatch.setattr(bench.time, "sleep", lambda s: threading.Event().wait(0.002))
    out = bench.first_audio_while_streaming_ms(
        "http://tts", "ws://stt/stream", "", "af_heart", np.zeros(160, "<i2"), 16_000, 5
    )
    assert out == [50.0] * 5
    assert len(measured) == 6  # one warm-up
    assert streamed and set(streamed) == {"ws://stt/stream"}
    after = len(streamed)
    threading.Event().wait(0.05)
    assert len(streamed) == after  # the streaming client stopped


def test_concurrent_finalize_runs_the_clients_at_once(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's clients overlap, each sends its utterances, and every
    finalize is reported (ADR 0020)."""
    import threading

    import numpy as np

    lock = threading.Lock()
    inside = 0
    most = 0
    leads: list[int] = []

    def stream_and_finalize(
        ws_url: str, api_key: str, stream: bytes, *rest: Any
    ) -> tuple[str, float]:
        nonlocal inside, most
        with lock:
            inside += 1
            most = max(most, inside)
            leads.append(len(stream))
        threading.Event().wait(0.02)
        with lock:
            inside -= 1
        return "text", 7.0

    monkeypatch.setattr(bench, "stream_and_finalize", stream_and_finalize)
    monkeypatch.setattr(bench.time, "sleep", lambda s: threading.Event().wait(0.001))
    out = bench.concurrent_finalize_ms("ws://stt", "", np.zeros(160, "<i2"), 16_000, 3, 4, 0.2)
    assert out == [7.0] * 12
    assert most == 3
    assert len(set(leads)) == 12, "every utterance ends at another phase"


def test_each_extra_tts_server_is_named_with_its_url_and_voice(bench: ModuleType) -> None:
    assert bench.extra_tts("OmniVoice=http://h:8850,en-US-female") == bench.ExtraTTS(
        "OmniVoice", "http://h:8850", "en-US-female"
    )
    assert bench.extra_tts("Phonon=http://h:8860").voice == "default"
    for bad in ("http://h:8860", "Phonon=", "=http://h"):
        with pytest.raises(argparse.ArgumentTypeError):
            bench.extra_tts(bad)


def test_the_combined_memory_row_is_against_the_budget(bench: ModuleType) -> None:
    stt = {"footprint_bytes": 1_700_000_000, "rss_bytes": 1}
    row = bench.combined_row("OmniVoice", stt, {"footprint_bytes": 2_100_000_000, "rss_bytes": 1})
    assert row == bench.Row("STT + TTS (OmniVoice) footprint, idle after use", "3.80 GB", "<= 3 GB")
