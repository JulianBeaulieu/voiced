# 0016: Verification results

Date: 2026-10-15. Status: accepted.

## Context

Before voiced is installed on Macs other than its authors', it was checked by
someone who wrote none of it, from the point of view of a stranger who
installs it on a Mac they care about and binds it to a home LAN. This record
says what was checked, how, what was found, what changed and what stays a
documented limit. Every fix has a regression test at the lowest tier that
catches it; each new test was also run against the code before the fix and
fails or hangs there.

## How

- Code review of every transport, the control channel, the installer and
  the watchdog, against the ADRs.
- Live instances on ephemeral ports: fake engines for the protocol checks,
  the real Parakeet and Kokoro engines (M1 Max, macOS 15.7.4) for the
  robustness checks.
- `pip-audit` (PyPI and OSV databases) over every pin in `uv.lock`,
  `pip-licenses` over a runtime environment built from it, and the Hugging
  Face model cards.
- `gitleaks` over the tree and the history of `main`, and pattern scans for
  personal data, emoji, attribution and process wording in the tree, blobs
  and commit messages.
- The README quickstart, run command by command in an empty `HOME` with no
  uv or Hugging Face cache, everything except the root step.

## Findings and changes

Severity is for an instance bound to a LAN, which the README documents.

| # | Severity | Finding | Change |
|---|---|---|---|
| 1 | high | A Wyoming client could make a service buffer any amount of memory: the `wyoming` reader reads whatever `payload_length` or `data_length` a header announces before the handler sees the event, so the 1 MB chunk cap ran too late. | Own bounded reader in `VoicedEventHandler.run`: sizes are refused from the header (ADR 0011 amendment). |
| 2 | high | Idle connections were never closed (Wyoming waited for the next event forever; uvicorn has no timeout before a request's first byte), and launchd gives a daemon 256 file descriptors, shared by both listeners, model files and `/health`. A client holding a few hundred connections stops the service answering anything; the watchdog then restarts it and the client repeats. | Wyoming: five minutes of silence closes a connection, the rest of an event must arrive within `session_idle_s`, at most 64 connections per listener. Services raise their soft limit to 4096. HTTP idle connections remain a limit (below). |
| 3 | medium | A Wyoming `AudioStart` with a mistyped field (`"rate": "16000"`) raised `TypeError` inside the slot: a failure, so `/health` turned `degraded`, and the watchdog restarts a service that stays degraded for two minutes. Any client could keep restarting the services. A rate of 1 Hz made each input sample 16000 output samples. | `PCMConverter` checks types and bounds (8 to 192 kHz, 1 to 16 channels); event parsing reports missing or mistyped fields as `invalid_request_error`. |
| 4 | medium | ffmpeg picks the demuxer from the upload's content. An `ffconcat` list made it open other files next to the upload, which was a named file in the temporary directory (`/tmp` for a daemon), so a client could have another audio file there transcribed. ffmpeg 9 refuses an HLS playlist with a `.bin` name, but that is ffmpeg's choice, not voiced's. | The upload is an anonymous temporary file, passed to ffmpeg as a descriptor with `-protocol_whitelist fd`; nothing else can be opened. `voiced doctor` fails an ffmpeg without the `fd` protocol. |
| 5 | medium | Decoded audio was unbounded: a 50 MB upload can decode to many hours of float32 at 16 kHz, all held in memory. Decoding also ran outside the queue, so any number of uploads could decode at once, each with its bytes, an ffmpeg process and its audio. | ffmpeg's output is counted while it is read and stopped past `max_upload_mb` of 16-bit 16 kHz audio (413). At most `queue_depth` uploads decode at once (503). |
| 6 | medium | The WebSocket stream had no audio or wall-clock bound, only the 30 s idle timeout: a client sending a frame every 29 s held a slot forever, and `queue_depth` such clients held them all. | The same bounds as a Wyoming utterance. |
| 7 | medium | A killed service could leave an upload file behind (the watchdog restarts with SIGKILL). | Fixed by 4: the file has no name. |
| 8 | medium | `voiced install` under `sudo`, or with `--user root`, produced daemons running the user-writable venv as root, the escalation ADR 0012 rules out for the watchdog. | Refused, before anything is written. |
| 9 | medium | Without a checkout and without `--source`, `voiced install` installs `voiced==<version>` from PyPI, where the name is unclaimed. The README's `uv tool install` copies the package, so this was the default for anyone running `voiced install` without `--source`, including the documented `--dry-run`. | Refused until `PUBLISHED` is set at the first release; README and CONTRIBUTING updated. Complements the open change that fixes the PEP 508 form of a pinned requirement. |
| 10 | medium | The config file was created 0644. macOS home directories are `drwxr-x---` with group `staff`, which every local account is in, so other accounts could read a hand-edited `api_key`. (`settings set api_key` already wrote 0600.) | Created 0600; the README says why. |
| 11 | low | With `api_key` set, `/metrics.json` withheld error details but `/health` and the `health` block of `/metrics.json` still showed `last_error` in full. Wyoming failures were not logged at all, only counted. | `ServiceContext.public_health()` keeps the exception name when a key is set; `inference()` logs every failure. |
| 12 | low | Wyoming passed a voice name straight to the engine, without the configured-language filter HTTP applies, and a non-string name could raise inside the slot. Not reachable with Kokoro, which loads English voices only. | Checked against `ServiceContext.voices()` first. |
| 13 | low | The engine catalogue child process ran `python -m` with the watchdog's working directory, the user's home, first on `sys.path`, so a stray `json.py` there shadowed the standard library. | `python -P`. |
| 14 | low | `voiced doctor` reported the interpreter running it, not the one the daemons run; `voiced status` before the root step gave no next step. | A "Service Python" check; a hint in `status`. |
| 15 | docs | Quickstart: `uv tool install` without `--python` fails where uv's default interpreter is 3.10 or 3.14 (seen on a second machine); "about 3 GB" was 2.7 GB of models plus about 1.7 GB of environments and caches; the `--dry-run` line lacked `--source`; the `openai` example needs a package the install does not include; the root step does more than the README said (it unloads running daemons first). | README quickstart rewritten from the run. |
| 16 | docs | Licences: `phonemizer` (GPL-3.0-or-later) is imported by the Kokoro engine in the tts process; `soxr` and `soundfile`'s `libsndfile` are LGPL-2.1-or-later; `certifi` and parts of `tqdm` are MPL-2.0. None were listed. Everything the README did list is correct, including the model licences. | README licence section. |
| 17 | docs | ADR 0007 said `max_input_chars` bounds a speech response to "a few minutes, about 13 MB". That holds for prose (4096 characters of the README: 6 minutes, 17 MB, 27 s of GPU time), but normalization expands symbols and digits: 4096 characters of the one-half sign (U+00BD) produced 35 minutes of audio (100 MB, 85 s of GPU time) and 4096 digits 19 minutes. | ADR 0007 corrected; kept as a limit (below). |

## Checked and found sound

- **Bind and discovery.** Loopback by default; `0.0.0.0` is an explicit
  setting with the security note; zeroconf is skipped for loopback binds.
- **API key.** Every `/v1` route and the WebSocket handshake go through one
  dependency using `hmac.compare_digest`. Open without a key: `/health`, `/`,
  `/metrics.json`, `/docs`, `/openapi.json`.
- **Status page settings form.** Always needs the key as a bearer token, so
  DNS rebinding and cross-site requests get nothing (a cross-site page cannot
  set `Authorization` without a CORS preflight, and there is no CORS); POST
  must be `application/json`.
- **TTS input.** `max_input_chars` is enforced on both transports; control
  characters are stripped before espeak (a request with NUL, BEL, ESC and DEL
  synthesized normally); `normalize` plus `split_sentences` stay under 0.2 s
  on 4096-character pathological inputs (repeated digits, separators,
  brackets, Markdown, random printable text); `speed` is bounded to 0.25 to
  4 by the engine base class; a 4096-character word, RTL overrides and
  combining marks, emoji-only and punctuation-only input all answer 200 with
  health `ok`.
- **Control channel.** The state directory is made 0700 and the socket 0600;
  the peer uid must equal the server's or the connection closes before
  `hello`; request lines are capped at 64 KiB, a client's queued replies at
  8 MiB, `logs.tail` at 4 MiB of the file and 4 KiB per line. A hostile
  process with the same uid can do anything the user can, including editing
  the config file directly, so the channel adds no capability for it. Not
  tested with a second uid: creating one needs root.
- **The root step.** `install-daemons.sh` runs `launchctl bootout`, `print`
  and `bootstrap`, `cp`, `chown`, `chmod`, `seq`, `sleep` and `echo`; it
  writes only the three plists under `/Library/LaunchDaemons`; every path is
  quoted. It executes nothing from the user's home. The daemons run as the
  installing user (`UserName`), with a fixed `PATH`, `HOME` and
  `VOICED_CONFIG`.
- **Dependencies.** `pip-audit` reports no known vulnerabilities in any of the
  98 pins of the full lock, or the 82 runtime pins, on either database.
- **Repository hygiene.** `gitleaks` finds nothing in the tree or the history
  of `main`. No personal hosts, addresses, emoji or AI attribution in the
  tree, its history or commit messages. Squash merges kept `main` to one
  commit per change. Older squash commit bodies on `main` describe how the
  work was organised; they hold nothing personal or secret and stay, since
  removing them means rewriting the public history.

## Robustness

Run on the M1 Max with the real engines, outside launchd:

| Check | Result |
|---|---|
| Two clients at warm-up | Connections refused while Python imports, then answered once the model was warm (3.8 s load); both got audio after 4.4 s. |
| `kill -9` during a streamed mp3 reply | The client sees a truncated chunked response (`IncompleteRead`); the ffmpeg child exits with its parent. |
| `kill -9` during an upload | No upload file left in `TMPDIR`, no ffmpeg left running. |
| SIGSTOP | `voiced watchdog --once` reports the service unreachable after its 5 s timeout. The restart itself (SIGKILL, then `kickstart`) needs installed daemons and was not run here; its decision rules are unit tested (ADR 0012). |
| Log disk full (2 MB disk image) | 20 of 20 requests answered 200; each lost log record is reported on stderr. |
| Models missing, `HF_HUB_OFFLINE=1` | Exits 1 within a second with `LocalEntryNotFoundError` in the log; under launchd it restarts every 10 s until the models can be downloaded. |
| Models cached, network unreachable | Healthy in 3 s from the cache. |

## Limits kept, and why

- **HTTP connections that never send a request** stay open: uvicorn has no
  timeout before the first byte of a request. With the open file limit at
  4096 a client needs thousands of connections, and a LAN bind is documented
  as trusted-network only. A real fix belongs in the server (a header timeout
  or a connection limit); voiced does not replace uvicorn's protocol layer.
- **Wyoming has no authentication.** Anyone who can reach the ports can use
  the models and keep them busy, and fill the 64 connection slots per
  listener. The bounds above stop one client from taking the process down,
  not from occupying it. This is the protocol, not something voiced can fix.
- **Normalization expands text** (finding 17): `max_input_chars` bounds the
  input, not the audio. Bounding the normalized text instead would refuse
  ordinary replies full of numbers, and `queue_depth` bounds how many such
  requests run. A slow reader can hold up to about 100 MB of finished audio
  per request.
- **Without an API key, web pages can send requests** to a loopback service
  (a form post to `/v1/audio/transcriptions`), but cannot read the answer
  without DNS rebinding. The README tells users to set a key.
- **A same-uid process can change the staged plists** between the review and
  the root step, or the script itself. Making the script self-contained
  would not help, since the script is equally writable; anything running as
  the user can already wrap `sudo`.
- **`/docs`** loads Swagger UI from a CDN in the browser that opens it. It is
  FastAPI's default and only touches the browser, not the server.

## Consequences

- ffmpeg must support its `fd` protocol (current Homebrew builds do);
  `voiced doctor` fails older ones.
- `voiced install` needs `--source` until the first PyPI release, and the
  release checklist gains setting `PUBLISHED`.
- New regression tests: `tests/wyoming/test_limits.py`, and additions to
  `tests/api/test_ffmpeg.py`, `test_transcriptions.py`, `test_stream_ws.py`,
  `test_status.py`, `tests/launchd/test_install.py`,
  `tests/control/test_ops.py`, `tests/cli/test_doctor.py` and
  `tests/cli/test_lifecycle.py`.
