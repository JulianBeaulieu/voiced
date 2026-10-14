# Home Assistant setup

voiced speaks the Wyoming protocol, which is how Home Assistant Assist talks
to local speech services. The stt service listens on TCP 10300 and the tts
service on TCP 10200. Both run inside the same processes as the HTTP API and
share the loaded models, so there is nothing extra to start.

Home Assistant 2025.7 or newer gets streaming text-to-speech; older
versions work with whole-message synthesis.

## Before you start

Find the address of the Mac running voiced. Open **System Settings**,
**Wi-Fi** (or **Network**), click **Details** next to the active connection
and read the IP address, or run:

```sh
ipconfig getifaddr en0
```

This guide calls it `<mac-ip>`. Give the Mac a fixed address in your router
so it does not change; mDNS discovery works without one, but a manual
integration entry stores the address.

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

If nothing is discovered (mDNS does not cross VLANs, or you disabled it with
`zeroconf = false`), add them by hand:

1. **Settings**, **Devices and services**, **Add integration**.
2. Search for **Wyoming Protocol** and select it.
3. Host `<mac-ip>`, port `10300`. Submit. This is speech-to-text.
4. Repeat with port `10200` for text-to-speech.

Both entries are titled "voiced"; the port tells them apart. Each one creates
an entity (`stt.voiced` and `tts.voiced`) that the pipeline uses.

<!-- TODO(phase2): verify the entity ids against a real Home Assistant -->

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
| Not discovered | Confirm `bind = "0.0.0.0"` and that `http://<mac-ip>:8880/health` opens from another machine. Add by host and port if mDNS is blocked between networks. |
| Integration added, but "Unavailable" | The service is still loading its model (`/health` says `starting`), or the Mac is waiting at the FileVault unlock screen after a reboot; see [operations.md](operations.md#filevault). |
| STT returns nothing | The pipeline language is not in the configured `languages` list. Version 1 is English only. |
| TTS cuts off | Check `voiced logs tts` for `queue_full`. Raise `queue_depth` under `[tts]` if many satellites talk at once. |
| Voices missing from the picker | Only voices whose language matches the configured `languages` are advertised. |

Logs: `voiced logs stt` and `voiced logs tts`, or the files under
`~/Library/Logs/voiced/`. The STT listener reports what Home Assistant sent
(rate, width, channels) at debug level; set `log_level = "debug"` under
`[server]` to see it.

The Wyoming message flow for other clients is documented in the
[API reference](api.md#wyoming-protocol).
