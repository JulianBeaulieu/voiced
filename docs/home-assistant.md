# Home Assistant setup

voiced speaks the Wyoming protocol, which is how Home Assistant Assist talks
to local speech services. The stt service listens on TCP 10300 and the tts
service on TCP 10200. Both run inside the same processes as the HTTP API and
share the loaded models, so there is nothing extra to start.

Home Assistant 2025.7 or newer gets streaming text-to-speech, so speech
starts while the conversation agent is still writing its reply. Older
versions work with whole-message synthesis.

## Before you start

Find the address of the Mac running voiced. Open **System Settings**,
**Wi-Fi** (or **Network**), click **Details** next to the active connection
and read the IP address, or run:

```sh
ipconfig getifaddr en0
```

This guide calls it `<mac-ip>`. Give the Mac a fixed address in your router
so it does not change; mDNS discovery works without one, but a manually
added integration stores the address.

## 1. Expose voiced on your network

Read the security note in the
[README](../README.md#security-note-on-bind) first: the Wyoming protocol has
no authentication, so only do this on a trusted network.

By default voiced listens on `127.0.0.1` and Home Assistant on another
machine cannot reach it. In the menu bar app, open **Settings**,
**Network**, set **Listen on** to **Local network** and accept the restart
the app offers. Without the app, in `~/.config/voiced/config.toml`:

```toml
[server]
bind = "0.0.0.0"
```

Restart both services so they pick it up; the services read the config when
they start, so this needs no root:

```sh
voiced restart all
```

Check from any other machine on the network by opening
`http://<mac-ip>:8880/health` in a browser. You should see a JSON document
with `"status": "ok"`. Then try `http://<mac-ip>:8881/health` for the stt
service.

## 2. Add the Wyoming integrations

With `bind = "0.0.0.0"`, voiced advertises `voiced-stt` and `voiced-tts` over
mDNS (zeroconf) and Home Assistant discovers them on its own:

1. Open **Settings**, then **Devices and services**.
2. Two **Wyoming Protocol** cards appear under **Discovered**, one per
   service. Click **Add** on each and confirm.

If nothing is discovered (mDNS does not cross VLANs, or you set
`zeroconf = false`), add them by hand:

1. **Settings**, **Devices and services**, **Add integration**.
2. Search for **Wyoming Protocol** and select it.
3. Host `<mac-ip>`, port `10300`. Submit. This is speech-to-text.
4. Repeat with port `10200` for text-to-speech.

Both entries are titled "voiced"; the port tells them apart. The tts entry
shows as not ready while its model is loading and connects once it is warm.

## 3. Create or edit an Assist pipeline

1. **Settings**, **Voice assistants**.
2. Click **Add assistant**, or open the existing **Home Assistant** pipeline
   and click the pencil to edit it.
3. **Speech-to-text**: pick **voiced**. Language: **English**.
4. **Text-to-speech**: pick **voiced**. Language: **English**. Pick a voice;
   `af_heart` is the default. The voice id prefixes are explained in the
   [API reference](api.md#get-v1audiovoices).
5. **Conversation agent**: **Home Assistant** for the built-in intent
   matcher, or an LLM; see the next section.
6. Save, then click the three dots on the pipeline and **Set as preferred**
   if you want it as the default for satellites.

Test it from the same page with the **Start a conversation** (microphone)
button, or from any Assist dialog in the UI.

## 4. Optional: Ollama as the conversation agent

An LLM makes Assist understand free-form requests instead of exact phrases.
Ollama runs locally and has a native Home Assistant integration.

1. Install Ollama and pull a model that supports tool calling, for example
   `ollama pull qwen3:8b`. Make Ollama listen on the network
   (`OLLAMA_HOST=0.0.0.0`) if it is not on the Home Assistant machine. This
   guide calls that machine `<ollama-host>`.
2. **Settings**, **Devices and services**, **Add integration**, search for
   **Ollama**.
3. URL `http://<ollama-host>:11434`, then pick the model.
4. In the integration's options, enable **Assist** so the model can control
   devices, and keep the exposed-entities list short; large prompts slow down
   the first token.
5. Back in **Voice assistants**, edit your pipeline and set **Conversation
   agent** to the Ollama entry.

A full voice turn is then: the satellite streams audio to Home Assistant,
Home Assistant decides when you stopped talking and sends the audio to
voiced (STT), sends the transcript to Ollama, and streams Ollama's reply
sentence by sentence to voiced (TTS) while the model is still generating.

## 5. Satellites

Any Wyoming or ESPHome voice satellite works, including Home Assistant Voice
Preview Edition and the Assist microphone in the companion app. Assign the
pipeline to the satellite under **Settings**, **Devices and services**, the
satellite's device page, **Voice assistant**.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Not discovered | Confirm `bind = "0.0.0.0"` and that `http://<mac-ip>:8880/health` opens from another machine. Add by host and port if mDNS is blocked between networks. |
| Integration added, but not ready | The service is still loading its model (`/health` says `starting`), or the Mac is waiting at the FileVault unlock screen after a reboot; see [operations.md](operations.md#filevault). |
| STT returns nothing or an error | The pipeline language does not match the configured `languages`. Version 1 is English only. |
| Error `server_busy` in the Home Assistant log | The inference queue was full or the model was loading. `stt.streams` (default 2) satellites are transcribed at once and the next waits; after about 34 s of waiting (its 64 queued chunks, then `server.session_idle_s`) it gets `server_busy`. Raise `queue_depth` if many satellites talk at once. |
| Voices missing from the picker | Only voices in a configured language are advertised. |

Logs: `voiced logs stt` and `voiced logs tts`, or the files under
`~/Library/Logs/voiced/`. The tts service logs one line per Wyoming
synthesis with the text length and voice; `log_level = "debug"` under
`[server]` adds connection events.

The Wyoming message flow, limits and error codes for other clients are in
the [API reference](api.md#wyoming-protocol).
