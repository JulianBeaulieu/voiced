# 0016: Verification results

Date: 2026-10-15. Status: accepted.

## Context

Before voiced is installed on Macs other than its authors', it was checked
from the point of view of a stranger who installs it on a Mac they care
about and binds it to a home LAN. This record says what was checked, how,
what was found, what changed and what stays a documented limit. Every fix
has a regression test at the lowest tier that catches it; each new test was
also run against the code before the fix and fails or hangs there.

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
  personal data, emoji, attribution and wording in the tree, blobs and
  commit messages.
- The README quickstart, run command by command in an empty `HOME` with no
  uv or Hugging Face cache, everything except the root step.

## Findings and changes

Severity is for an instance bound to a LAN, which the README documents. Test
names are in `tests/`; `limits` is `tests/wyoming/test_limits.py`.

| # | Sev | Finding | Reproduction | Change | Test |
|---|---|---|---|---|---|
| 1 | high | The `wyoming` reader reads whatever `payload_length` or `data_length` a header announces before the handler sees the event, so the 1 MB chunk cap ran too late: any client could make a service buffer any amount of memory. | A header announcing a 2 GB payload, then data: resident memory grows with what is sent. | Own bounded reader in `VoicedEventHandler.run`; sizes are refused from the header (ADR 0011 amendment). | `limits::test_event_is_refused_from_its_header` |
| 2 | high | Idle connections were never closed (Wyoming waited for the next event forever; uvicorn has no timeout before a request's first byte), and launchd gives a daemon 256 file descriptors, shared by both listeners, model files and `/health`. A client holding a few hundred connections stops the service answering anything; the watchdog restarts it and the client repeats. | 300 idle connections to the Wyoming port, then `GET /health`. | Wyoming: five minutes of silence closes a connection, the rest of an event must arrive within `session_idle_s`, at most 64 connections per listener. `voiced serve` raises its soft limit to 4096 before it accepts a connection. | `limits::test_idle_connection_is_closed`, `test_half_sent_event_is_closed_after_session_idle`, `test_connections_over_the_cap_are_refused_and_counted_again_when_freed`; `test_cli.py::test_open_file_limit_is_raised`, `test_serve_raises_the_limit_before_serving` |
| 3 | medium | A Wyoming `AudioStart` with a mistyped field raised `TypeError` inside the slot: a failure, so `/health` turned `degraded`, and the watchdog restarts a service that stays degraded for two minutes. Any client could keep restarting the services. A rate of 1 Hz made each input sample 16000 output samples. | `AudioStart` with `"rate": "16000"`, then a chunk; `/health` says `degraded`. | `PCMConverter` checks types and bounds (8 to 192 kHz, 1 to 16 channels); event parsing reports missing or mistyped fields as `invalid_request_error`. | `limits::test_bad_audio_format_is_a_client_error_and_health_stays_ok` (5 cases) |
| 4 | medium | Deeply nested JSON in a Wyoming header raised `RecursionError`, which the reader did not catch: an unhandled exception with a traceback in the log for every connection, free to any client. | A header line of 60 000 `[`. | Caught with `ValueError`; the event is refused. | `limits::test_header_that_is_not_json_is_refused` |
| 5 | medium | ffmpeg picks the demuxer from the upload's content. An `ffconcat` list made it open other files next to the upload, a named file in the temporary directory (`/tmp` for a daemon), so a client could have another audio file there transcribed. ffmpeg 9 refuses an HLS playlist with a `.bin` name, but that is ffmpeg's choice, not voiced's. | Upload `ffconcat version 1.0` / `file other.wav` with `other.wav` in the temp directory: its transcript comes back. | The upload is an anonymous temporary file, passed to ffmpeg as a descriptor with `-protocol_whitelist fd`; nothing else can be opened. `voiced doctor` fails an ffmpeg without the `fd` protocol. | `api/test_ffmpeg.py::test_upload_cannot_make_ffmpeg_open_other_files`; `cli/test_doctor.py::test_external_programs` |
| 6 | medium | Decoded audio was unbounded: a small upload can decode to hours of float32 at 16 kHz, all held in memory. Decoding ran outside the queue, so any number of uploads decoded at once. | A 3-hour FLAC of silence: peak memory in the GBs. | ffmpeg's output is counted while it is read and stopped past `max_upload_mb` of 16-bit 16 kHz audio (413). At most `queue_depth` uploads decode at once (503, `TooManyUploads`). | `api/test_ffmpeg.py::test_decoded_length_is_bounded`; `api/test_transcriptions.py::test_upload_that_decodes_to_too_much_audio_is_413`, `test_concurrent_decodes_are_bounded` |
| 7 | medium | The WebSocket stream had no audio or wall-clock bound, only the 30 s idle timeout: a client sending a frame every 29 s held a slot forever, and `queue_depth` such clients held them all. | One frame every 29 s. | The same bound as a Wyoming utterance, in one place (`voiced.inference.UtteranceBudget`). | `api/test_stream_ws.py::test_stream_audio_is_bounded`, `test_stream_session_has_a_wall_clock_bound`; `core/test_inference.py::test_utterance_budget_*` |
| 8 | medium | A killed service could leave an upload file behind (the watchdog restarts with SIGKILL). | SIGKILL during a decode; the file stays in the temp directory. | Fixed by 5: the file has no name. | `api/test_ffmpeg.py::test_upload_leaves_no_file_behind` |
| 9 | medium | `voiced install` under `sudo`, or with `--user` naming uid 0 (`root`, `System Administrator` or any alias), produced daemons running the user-writable venv as root, the escalation ADR 0012 rules out for the watchdog. `--user` naming anyone else ran this user's venv as that account. The default user came from `getpass.getuser()`, which believes `LOGNAME`, so `LOGNAME="System Administrator"` did the same. | `voiced install --user root --dry-run` prints a plist with `UserName` root. | Running as root is refused before anything is written, and `--user` is removed: the daemons always run as the installing user, whose venv they run, resolved from the uid, not the environment. | `launchd/test_install.py::test_cli_install_refuses_unsafe_requests`, `test_plan_uses_the_real_account_not_logname` |
| 10 | medium | Without a checkout and without `--source`, `voiced install` installs `voiced==<version>` from PyPI, where the name is unclaimed. The README's `uv tool install` copies the package, so this was the default for anyone running `voiced install` without `--source`, including the documented `--dry-run`. | `voiced install --dry-run` after `uv tool install`: `package voiced==0.0.1[stt,tts]`. | Refused until `PUBLISHED` is set at the first release; README and CONTRIBUTING updated. | same test |
| 11 | medium | The config file was created 0644. macOS home directories are `drwxr-x---` with group `staff`, which every local account is in, so other accounts could read a hand-edited `api_key`. (`settings set api_key` already wrote 0600.) | `ls -l ~/.config/voiced/config.toml` after install. | Created 0600; re-running `voiced install` tightens an existing one; `voiced doctor` warns on a readable one. | `launchd/test_install.py::test_write_default_config`, `test_run_install_tightens_an_existing_readable_config`; `cli/test_doctor.py::test_config_file_mode` |
| 12 | low | With `api_key` set, `/metrics.json` withheld error details but `/health` and the `health` block of `/metrics.json` still showed `last_error` in full. Wyoming failures were not logged at all, only counted. | An engine failure whose message echoes the request, then `GET /health`. | `ServiceContext.public_health()` keeps the exception name when a key is set; `inference()` logs every failure with its traceback. | `api/test_status.py::test_health_withholds_the_error_message_when_a_key_is_set` |
| 13 | low | Wyoming passed a voice name straight to the engine, without the configured-language filter HTTP applies, and a non-string name could raise inside the slot. Not reachable with Kokoro, which loads English voices only. | `Synthesize` with a voice in an unconfigured language, or `"name": 7`. | `ServiceContext.check_voice`, used by both transports. | `limits::test_tts_bad_voice_or_text_is_a_client_error`, `test_tts_voice_outside_the_configured_languages_is_refused`, `test_tts_stream_that_cannot_start_fails_once_and_closes` |
| 14 | low | The engine catalogue child process ran `python -m` with the watchdog's working directory, the user's home, first on `sys.path`, so a stray `json.py` there shadowed the standard library. | `~/json.py` that exits; `engines.list` fails. | `python -P` (and in `doctor`'s probes). | `control/test_ops.py::test_engines_in_subprocess_ignores_modules_in_the_working_directory` |
| 15 | low | `voiced doctor` reported the interpreter running it, not the one the daemons run, and called a venv whose `python` link broke "not installed yet"; `voiced status` before the root step gave no next step. | `uv python uninstall 3.12`, then `voiced doctor`. | A "Service Python" check that fails a broken link; a hint in `status`. | `cli/test_doctor.py::test_service_python_*`; `cli/test_lifecycle.py::test_status_says_what_to_do_before_the_daemons_are_installed` |
| 16 | docs | Quickstart: `uv tool install` without `--python` fails where uv's default interpreter is 3.10 and installs on an unsupported Python where it is 3.14; "about 3 GB" was 2.7 GB of models plus about 1.7 GB of environments and caches; the `--dry-run` line lacked `--source`; the `openai` example needs a package the install does not include; the root step does more than the README said (it unloads running daemons first). | The quickstart below. | README quickstart rewritten from the runs. | |
| 17 | docs | Licences: `phonemizer` (GPL-3.0-or-later) is imported by the Kokoro engine in the tts process; `soxr` and `soundfile`'s `libsndfile` are LGPL-2.1-or-later; `certifi` and parts of `tqdm` are MPL-2.0. None were listed. Everything the README did list is correct, including the model licences. | `pip-licenses` over the runtime environment. | README licence section, with what shipping a built tts environment means. | |
| 18 | docs | ADR 0007 said `max_input_chars` bounds a speech response to "a few minutes, about 13 MB". That holds for prose (4096 characters of the README: 6 minutes, 17 MB, 27 s of GPU time), but normalization expands symbols and digits: 4096 characters of the one-half sign (U+00BD) produced 35 minutes of audio (100 MB, 85 s of GPU time) and 4096 digits 19 minutes. | `POST /v1/audio/speech` with those inputs. | ADR 0007 corrected; kept as a limit (below). | |

## Quickstart

Run twice in an empty `HOME`, on an M1 Max with macOS 15.7.4, without the
root step (the services were started by hand with the plists' environment):

| Step | First run | Second run |
|---|---|---|
| `brew install espeak-ng ffmpeg uv` (bottles, fetched only) | 79 s, about 79 MB | |
| `git clone` | 2 s, 3.2 MB | |
| `uv tool install --python 3.12 ~/voiced` | 8 s, 114 MB including a uv-managed Python | |
| `voiced install --source ~/voiced` | 4 min 23 s: models 2.7 GB, `~/.voiced` 700 MB, uv cache 782 MB | |
| Whole quickstart | about 5 minutes, about 4.4 GB on disk | 6 minutes, 4.2 GB |
| Services healthy with the models on disk | stt 5 s, tts 7 s | |
| README `curl` speech and transcription | 0.20 s and 0.27 s, a valid 24 kHz WAV and its transcript | every step worked |

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
  not from occupying it: a client that keeps 64 connections open, sending
  an event every few minutes or reconnecting as each one closes, keeps Home
  Assistant out of that listener for as long as it likes. This is the
  protocol, not something voiced can fix; per-address limits would only
  move the number.
- **A refused Wyoming event may never reach the client as an error.** The
  listener answers an oversize header with one `Error` and closes; if the
  client is still sending the body, the close can reset the connection
  before it reads that `Error`. It still sees the connection close.
- **Normalization expands text** (finding 18): `max_input_chars` bounds the
  input, not the audio. Bounding the normalized text instead would refuse
  ordinary replies full of numbers, and `queue_depth` bounds how many such
  requests run. A slow reader can hold up to about 100 MB of finished audio
  per request.
- **Upload bodies are read before admission.** The multipart body is read
  (spooled to disk past 1 MB, cut off at `max_upload_mb`) before an upload
  is admitted to decoding, so how many bodies are being received at once is
  unbounded, as it was before. Each costs disk and a connection, not memory
  or ffmpeg; admitting before reading would need a connection-level limit
  in front of the routes.
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
