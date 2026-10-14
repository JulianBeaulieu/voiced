# Home Assistant setup

voiced speaks the Wyoming protocol, which is how Home Assistant Assist talks
to local speech services. The stt process listens on TCP 10300 and the tts
process on TCP 10200. Both run inside the same processes as the HTTP API and
share the loaded models, so there is nothing extra to start.

This guide uses `<mac-ip>` for the address of the Mac running voiced and
`<ollama-host>` for the machine running Ollama. Replace them with your own.
Home Assistant 2025.7 or newer gets streaming text-to-speech; older
versions work with whole-message synthesis.

## 1. Expose voiced on your network

By default voiced listens on `127.0.0.1` and Home Assistant on another
machine cannot reach it. In `~/.config/voiced/config.toml`:

```toml
[server]
bind = "0.0.0.0"
```

Then `voiced stop` and `voiced start`. Read the security note in the
[README](../README.md#security-note-on-bind) first: the Wyoming protocol has
no authentication, so only do this on a trusted network.

Check from the Home Assistant host (or any other machine):

```sh
nc -vz <mac-ip> 10300
nc -vz <mac-ip> 10200
```

## 2. Add the Wyoming integrations

With `bind = "0.0.0.0"`, voiced advertises `voiced-stt` and `voiced-tts` over
mDNS (zeroconf) and Home Assistant discovers them on its own:

1. Open **Settings**, then **Devices and services**.
2. Two **Wyoming Protocol** cards appear under **Discovered**, one per
   service. Click **Add** on each and confirm.

If nothing is discovered (mDNS does not cross VLANs, or you disabled it with
`zeroconf = false`), add them by hand:

1. **Settings**, **Devices and services**, **Add integration**.
2. Search for **Wyoming Protocol** and select it.
3. Host `<mac-ip>`, port `10300`. Submit. This is speech-to-text.
4. Repeat with port `10200` for text-to-speech.

Both entries are titled "voiced"; the port tells them apart. Each one creates
an entity (`stt.voiced` and `tts.voiced`) that the pipeline uses.
<!-- TODO(phase2): verify entity ids once merged -->

## 3. Create or edit an Assist pipeline

1. **Settings**, **Voice assistants**.
2. Click **Add assistant**, or open the existing **Home Assistant** pipeline
   and click the pencil to edit it.
3. **Speech-to-text**: pick **voiced**. Language: **English**.
4. **Text-to-speech**: pick **voiced**. Language: **English**. Pick a voice;
   `af_heart` is the default. Voices starting with `af_` and `am_` are
   American English, `bf_` and `bm_` are British.
5. **Conversation agent**: **Home Assistant** for the built-in intent
   matcher, or an LLM; see the next section.
6. Save, then click the three dots on the pipeline and **Set as preferred**
   if you want it as the default for satellites.

Test it from the same page with the **Start a conversation** (microphone)
button, or from any Assist dialog in the UI.

## 4. Ollama as the conversation agent

An LLM makes Assist understand free-form requests instead of exact phrases.
Ollama runs locally and has a native Home Assistant integration.

1. Install Ollama and pull a model that supports tool calling, for example
   `ollama pull qwen3:8b`. Make Ollama listen on the network
   (`OLLAMA_HOST=0.0.0.0`) if it is not on the Home Assistant machine.
2. **Settings**, **Devices and services**, **Add integration**, search for
   **Ollama**.
3. URL `http://<ollama-host>:11434`, then pick the model.
4. In the integration's options, enable **Assist** so the model can control
   devices, and keep the exposed-entities list short; large prompts slow down
   the first token.
5. Back in **Voice assistants**, edit your pipeline and set **Conversation
   agent** to the Ollama entry.

A full voice turn is then: satellite streams audio to Home Assistant, Home
Assistant streams it to voiced (STT), sends the transcript to Ollama, and
streams Ollama's reply sentence by sentence to voiced (TTS) while the model
is still generating. With a small model on the same LAN, expect roughly half
a second from end of speech to first audio.

## 5. Satellites

Any Wyoming or ESPHome voice satellite works, including Home Assistant Voice
Preview Edition and the Assist microphone in the companion app. Assign the
pipeline to the satellite under **Settings**, **Devices and services**, the
satellite's device page, **Voice assistant**.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Not discovered | Confirm `bind = "0.0.0.0"` and that `nc -vz <mac-ip> 10300` connects from the Home Assistant host. Add by host and port if mDNS is blocked between networks. |
| Integration added, but "Unavailable" | The service is still loading its model (`/health` says `starting`), or the Mac rebooted with FileVault on and is waiting at the unlock screen. |
| STT returns nothing | The pipeline language is not in the configured `languages` list. Version 1 is English only. |
| TTS plays the reply twice | Home Assistant older than 2025.7 together with a very old voiced build. Update voiced; the listener ignores the duplicate `Synthesize` Home Assistant sends inside a text stream. |
| TTS cuts off | Check `voiced logs tts` for `queue_full`. Raise `queue_depth` under `[tts]` if many satellites talk at once. |
| Voices missing from the picker | Only voices whose language matches the configured `languages` are advertised. |

Logs: `voiced logs stt` and `voiced logs tts`, or the files under
`~/Library/Logs/voiced/`. The STT listener reports what Home Assistant sent
(rate, width, channels) at debug level; set `log_level = "debug"` under
`[server]` to see it.

## Protocol notes

For people integrating other Wyoming clients:

- STT: `Describe` answers `Info` with one `AsrProgram` named `voiced`, the
  model id and the configured languages. Send `Transcribe`, `AudioStart`,
  `AudioChunk` events, then `AudioStop`; the answer is `Transcript`. Any
  sample width, channel count and rate is accepted and converted to mono
  16 kHz. `requires_external_vad` is true: the client decides when speech
  ends.
- TTS: `Info` lists one `TtsProgram` with a `TtsVoice` per voice and
  `supports_synthesize_streaming` true. Plain `Synthesize` answers
  `AudioStart`, `AudioChunk` events, `AudioStop` at 24 kHz, 16-bit, mono.
  Streaming `SynthesizeStart`, `SynthesizeChunk`, `SynthesizeStop` synthesizes
  sentence by sentence as text arrives and ends with `AudioStop` then
  `SynthesizeStopped`.
- Errors come back as a Wyoming `Error` event with `code` set to
  `queue_full`, `engine_error` or the engine exception name.

The reasoning behind these choices and what Home Assistant's integration
actually expects are in ADR 0004, `docs/decisions/0004-wyoming.md`.
<!-- TODO(phase2): link the ADR once PR 1 is merged -->
