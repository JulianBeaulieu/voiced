# 0002: Engine interfaces

Date: 2026-10-14. Status: accepted (frozen).

## Context

The HTTP layer, the Wyoming layer and the engines are built in parallel by
different people. They need one contract, fixed up front, and third parties
will later want to add Whisper, Piper, Pocket TTS and other models.

## Decision

Two abstract base classes in `voiced/engines/base.py`:

```python
class STTEngine:
    name: str; model_id: str; languages: list[str]; license: str
    async def load(self) -> None
    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript
    async def stream(self, language: str | None = None) -> STTStreamSession

class STTStreamSession:
    async def add_audio(self, samples: NDArray[np.float32]) -> None
    def partial(self) -> str
    async def finalize(self) -> Transcript
    async def close(self) -> None

class TTSEngine:
    name: str; model_id: str; languages: list[str]; license: str
    async def load(self) -> None
    async def voices(self) -> list[Voice]
    def synthesize(self, text: str, voice: str, speed: float = 1.0) -> AsyncIterator[PCMChunk]
```

Fixed audio formats: STT input is mono float32 at 16 kHz as a numpy array;
TTS output is mono int16 little-endian PCM at 24 kHz as bytes. Decoding
uploads and encoding responses is the API layer's job, never the engine's.

All engine methods are `async`. Engines wrap their blocking MLX calls in
`asyncio.to_thread` (amended by ADR 0005: use `voiced.threads.run_blocking`,
which keeps the slot held until the thread finishes even on cancellation). The server guarantees that calls on one engine never
overlap by routing every request through `InferenceQueue`, so engines do not
need their own locking. Cancellation follows asyncio: a disconnected client
cancels the awaiting task, and engines must clean up in `finally` and never
swallow `CancelledError`.

Errors that the client caused (`UnknownVoice`, `UnsupportedLanguage`, bad
input) are `EngineError` subclasses and become 4xx responses. Anything else
is an engine failure: 500, `degraded` in `/health`, and the watchdog may
restart the process.

Engines are discovered by entry points in the `voiced.stt_engines` and
`voiced.tts_engines` groups. The entry-point name is the `engine` value in
config. `FakeSTTEngine` and `FakeTTSEngine` in `voiced.testing` are the
reference implementations and ship registered as `fake`.

## Alternatives considered

- Synchronous engine methods with the server threading around them. Rejected
  because streaming TTS wants an async iterator anyway, and mixing sync and
  async on one interface is confusing for plugin authors.
- Passing file paths or bytes to `transcribe`. Rejected: engines should not
  each grow their own ffmpeg handling.
- A single `Engine` class with both modalities. Rejected: the two processes
  load different models, and most plugins will implement only one side.

## Consequences

- Changing these signatures means changing every engine, the HTTP layer
  and the Wyoming layer together; treat them as frozen.
- Engines that natively produce a different sample rate must resample
  themselves.
- Language lists are per engine and advertised through `/v1/models` and the
  Wyoming `Info` message, which is what makes adding German later a plugin
  concern rather than a server change.

## Amendment (ADR 0033)

Both interfaces gain `async def unload()`, whose default raises
`NotImplementedError`; no existing signature changed. An engine that
implements it drops every reference to its model and returns the memory
(`voiced.engines.memory.release_memory()`), and `load()` is called again
before any other method. `load()` is therefore no longer called exactly
once. See [ADR 0033](0033-model-residency.md).
