# 0013: Local control channel

Date: 2026-10-15. Status: accepted.

## Context

A menu bar app should show what voiced is doing, change the common
settings, restart a service and open the logs without a terminal. The CLI
wants the same things. None of this may be reachable from the network, and
none of it may need root: the daemons run as the installing user (ADR 0004)
and the installer never runs `sudo` (ADR 0006).

## Decision

### The watchdog hosts it

The watchdog (ADR 0012) already runs as the installing user for as long as
voiced is installed, already supervises both services, and already has the
only restart path that works without root (SIGKILL plus plain `launchctl
kickstart`). It serves `<state dir>/control.sock`.

The state dir is `[install] venv_dir` (`~/.voiced` by default), the one
per-user directory the install layout already owns. The socket therefore
moves with the venv: a client looking for it (the menu bar app) must read
`venv_dir` from the config file, or relocating the venv makes it look in
the wrong place. `settings.get` reports the directory as `paths.state`.

**One loop, on asyncio.** The watchdog loop is `Watchdog.run_async`, and
the socket is served on the same event loop. Each tick runs the same
blocking code as before (urllib, `launchctl`) in a background thread. The
alternative was to keep the synchronous loop and serve the socket from a
thread; that needs its own thread per client or a selector loop, plus
thread-safe fan-out of events, which is what asyncio's Unix server already
is. There is no second, synchronous loop.

**Locking.** A tick observes each service (`/health`, `launchctl print`,
the optional probe: up to `timeout_s` plus `probe_timeout_s`) without a
lock, then takes `Watchdog.lock` only to decide and, if needed, restart. A
requested restart takes the same lock, so it never interleaves with a
decision on the same `ServiceState`, and it waits at most for a decision or
restart in progress, never for a health poll.

**Requested restarts.** `service.restart` checks synchronously that the
service can be restarted (plist present, else `not_installed`; loaded in
launchd, else `service_unavailable`), answers `{"accepted": true}` at once
and runs the restart in a background task; completion shows in `status`
events. `all` tries both services: it is accepted if either can restart,
and `detail` gives the outcome per service. The restart goes through
`Watchdog.restart_now`, the same code a watchdog decision uses, so it counts
towards `restarts`, starts the startup grace period and is not followed by
an automatic restart while the model loads. The cooldown does not hold back
a requested restart.

**What the watchdog believes a service runs.** Each `ServiceSpec` carries
the engine, model and API key from the config file as they were when the
service last started. The watchdog re-reads the file for a service only
when that service starts again: after a restart it issued, or when
launchd hands it a new pid. Writing the file changes nothing in the
watchdog, so `status` never reports an engine the running process has not
loaded, and the health probe uses the key the running process expects.

**No engine code in the watchdog.** ADR 0012 wants the watchdog boring and
impossible to wedge. Engine plugins are third-party code that import numpy,
FastAPI (the test engines) and more. So `engines.list`, and the engine and
voice checks of `settings.set`, run `python -m voiced.engines.catalogue`
in a child process with an 8 s timeout (it takes a fraction of a second;
the CLI client waits 10 s, so it sees this error rather than its own
timeout) and cache the result for a minute; a plugin that hangs or crashes
there costs an error answer, not the supervisor. Two limits are deferred:
a failed read is not cached, so every call while a plugin hangs waits the
full timeout again; and one broken third-party plugin fails all of
`engines.list`, because the registry loads every entry point together. The `voiced` entry point imports the installer and `doctor`
(which import the engine modules) only inside the commands that use them.
What `voiced watchdog` does import: the standard library, `voiced.config`
and `voiced.settings_store` (pydantic, tomlkit) and `voiced.control`. A test
starts the watchdog's modules in a fresh interpreter and fails if FastAPI,
numpy or any `voiced.engines` module is loaded.

If the socket cannot start (another watchdog answers on it, the state
directory belongs to someone else, the path is too long for a Unix socket),
the error is logged to `watchdog.log` and printed to stderr, and the
watchdog runs without it: supervising the services matters more. `voiced
status` and `voiced doctor` then see the watchdog's pid in launchd with no
answering socket and say "watchdog running but control socket not served;
see `voiced logs watchdog`" instead of "not running".

When nothing answers on the socket, clients treat that as "voiced is not
running or not installed". The CLI then falls back: `voiced restart` runs
the same SIGKILL-and-kickstart itself, `voiced settings` runs the same
`Operations` in-process against the config file (no supervisor), and
`voiced status` shows launchd and `/health` only. A watchdog that accepts a
connection and then stops answering is reported as "watchdog did not
answer", not as a traceback.

### A Unix socket, not loopback HTTP

Loopback HTTP is reachable by every local user and by every web page the
user opens (DNS rebinding, `fetch` to `127.0.0.1`), and it changes meaning
when someone sets `bind = "0.0.0.0"`. A Unix socket has a filesystem
permission and a kernel-reported peer:

- the state directory is created (or tightened) to 0700, and the socket is
  0600; a directory owned by another uid is refused. The socket is chmodded
  right after `bind`, so for a moment it has the umask's mode; the 0700 parent
  keeps other users out of that window;
- every connection's peer uid (`LOCAL_PEERCRED` on macOS, `SO_PEERCRED` on
  Linux) must equal the server's uid; anything else, including a platform
  that cannot say, is closed before the `hello` is sent;
- a socket file nobody answers on is a leftover from a crash and is removed
  at start; one that answers belongs to a running watchdog and is left
  alone; a file that is not a socket is never deleted.

The framing is newline-delimited JSON with request ids and server-pushed
events, so a Swift client needs nothing beyond Foundation and a shell user
can try it with `nc -U`.

### What is settable

`bind` (`loopback` or `lan`), `api_key`, `zeroconf`, `browser_settings`,
`stt.engine`, `stt.model`, `tts.engine`, `tts.model`, `tts.voice`
(`[tts] default_voice`) and `tts.speed`. Ports, log locations, the label
prefix, the watchdog policy and the install keys are not: changing them
needs the plists re-staged or the daemons re-installed, which is a root
step. `bind = "lan"` writes `0.0.0.0`, unless the file already has a
specific non-loopback address, which is kept.

`voiced.settings_store.apply_settings` is the only writer, for the socket,
the CLI fallback and the status page form. `voiced.config` stays the schema
and the loader.

- The would-be file contents are validated with the `Settings` section
  models, without environment overrides (an invalid file value must not
  hide behind a variable); an invalid value never reaches the file.
- An engine being selected must be in the registry, and a voice must be in
  the engine's catalogue. The registry lists entry points; an engine whose
  extra is not installed is still listed (a `voiced install` always has the
  extras). A new `stt.engine` or `tts.engine` without its `model` gets the
  engine's first catalogue model, since the old engine's model would not
  load and launchd would restart the service forever; a new `tts.engine`
  without `tts.voice` keeps the voice if the new engine has it, else gets
  the built-in default voice (`af_heart`) if the engine has that, else its
  first voice. The derived keys appear in the result's `changed`. An
  engine with no catalogue model must be given `model` in the same change.
- The file is edited with `tomlkit`: only the requested values that differ
  from the file's text are rewritten (a requested key absent from the file
  is written even when it equals its default, as long as something in the
  change takes effect); comments, key order, dotted keys, inline tables and every
  other key stay as they were. The edited text must parse back (with
  `tomllib`) to exactly the intended data or nothing is written. This is
  the one write path: there is no fallback that rewrites the file and drops
  comments. The trade is one runtime dependency (pure Python, no
  dependencies of its own) against a purpose-built line editor with a lossy
  fallback, which was about 120 lines and still lost comments silently for
  some layouts.
- It is written to a temporary file in the target's directory, fsynced,
  and renamed over it. A symlinked config file is followed, so the link
  stays and its target is replaced. The old mode is kept (0600 for a new
  file), except that a file holding a non-empty `api_key` is always made
  0600: the daemons run as the user, so nobody else needs to read it.
- `api_key` is written but never read back out: `settings.get` reports
  `api_key_set` only.

Engine catalogues come from `voiced.engines.catalogue`: models and
languages from each engine's `model_id` and `languages`, voices from an
optional `catalogue_voices()` classmethod returning `Voice` values without
loading weights (Kokoro returns its static voice table, Soprano its one
voice). It is not part of the frozen interface in `voiced.engines.base`, and
the running service's `voices()` remains the authority. Display labels are
built there, in one place: an engine's `label` is its name capitalised;
`voice_labels` maps each voice id to `Name (Language, gender)` when the
engine gives a gender (Kokoro's names, languages and genders come from its
voice id convention, `kokoro_voices()`), else to the voice's name (Soprano:
"Default"). `LANGUAGE_NAMES` there also names the languages Kokoro can
speak beyond English; voiced v1 is English-only, so `engines.list` only
lists English voices today, and the table is ready for when an engine
enables more.

### Restart rules

`settings.set` never restarts anything. It returns `restart_required`,
derived from which keys actually changed value:

| Changed | Restart |
|---|---|
| `[server]` keys: `bind`, `api_key`, `zeroconf` | `stt`, `tts` |
| `stt.*` | `stt` |
| `tts.*` | `tts` |
| `browser_settings` | none: the status page reads it from the file |

A value equal to the current one changes nothing and needs no restart.
Warnings: `lan_without_api_key` when `bind` or `api_key` was part of the
change and the result is a LAN bind with no key, and `env_override:<key>`
when a `VOICED_*` variable shadows the key just written.

A `bind` change to `lan` also turns on the zeroconf announcement of the
Wyoming listeners when `zeroconf` is on (ADR 0011), after the restart.

### Settings events

The watchdog watches the config file (modification time, size and inode,
`settings_store.file_stamp`, the same check the status page uses; checked
on every status poll while a client is connected, and right after a
`settings.set`). Whenever it changes, by `settings.set`, the status page
form or an editor, every connected client gets a `settings` event with the
`settings.get` payload. A `settings.set` that changes nothing writes nothing
and sends no event.

### The browser form

The status page can show a settings form, off by default
(`[server] browser_settings = false`). It calls the same function as the
socket. Rules, enforced on the server:

- off: `GET /settings.json` and `POST /settings` answer 404 with how to turn
  it on; the switch is read from the file (re-read when the file changes),
  so turning it off takes effect at once;
- **the API key is always required**, as a bearer token. With no key set
  the form is refused (403) with an explanation, on loopback too. A page
  the operator opens in their own browser can reach a loopback port, and
  with DNS rebinding it can make its requests same-origin, so neither the
  bind address nor an `Origin` check tells the operator from that page;
  only a secret does. While a key change waits for a restart, either the
  old or the new key is accepted;
- `POST` must be `application/json`.

The menu bar app and `voiced settings` use the socket and need no key.

### Protocol version policy

The `hello` event carries `protocol: 1`.

- Compatible (no version change): new operations, new optional arguments,
  new result fields, new event types, new warnings, new error codes. Clients
  ignore unknown fields and events. `warnings` and `error.code` are open
  sets: a client shows an unknown warning as plain text and treats an
  unknown error code as `internal`.
- Breaking (the version goes up): removing or renaming anything, changing
  a type or a meaning, a new required argument, and a new value in a
  closed set: `state`, `bind`, the members of `restart_required`, and
  `open.what`. The server then speaks only the new version.
- A client that sees a newer `protocol` than it knows refuses with a clear
  message telling the user to update the app, instead of guessing.

Behaviour outside the request/response shapes: a request whose `id`
cannot be read is answered with `"id": null`; a request line over 64 KiB,
or an incomplete line at end of stream, gets a `bad_request` answer with
`"id": null` and the connection is closed. The 64 KiB limit is for requests
only: replies can be longer (a 1000-line `logs.tail` is a few hundred KiB,
at most about 4 MiB), so clients read replies without that cap.

### Fairness and bounds

The watchdog serves every client from one event loop, so no client may
hold it up:

- Each client has its own send queue and writer task. Answers and events
  are only queued. While more than 1 MiB waits for a client, its next
  request is not read, so a client that asks without reading cannot pile
  up replies; a client with more than 8 MiB waiting (events keep coming),
  or that does not take a message within 5 s, is dropped and its queue
  freed. Another client's answers and events never wait for it.
- `status` reads the services in parallel, so a hung service costs one
  health check timeout (2 s), not one per service. `status` events keep
  the 10 s promise however long the reads take: the poll waits less as the
  deadline nears, and a read that ends within one read's length of the
  deadline pushes.
- `logs.tail` reads at most the last 4 MiB of the file (1000 lines of
  4 KiB), counting newlines once per block, and cuts any line over 4 KiB
  with ` [line truncated]`; a line whose start lies before those bytes
  begins with `[...] `. A log that is one 200 MB line costs the same as a
  short one.

Additions in this version beyond the original contract, all compatible:
`detail` in the `service.restart` result, `changed` in the `settings.set`
result, `path` in `logs.tail`, `languages` on TTS engines in
`engines.list`, `label` on every engine and `voice_labels` on TTS engines,
an optional `service` for `open status_page`, `field` in `invalid_setting`
errors and the `env_override:<key>` warning. Outside the protocol, the
Soprano voice's display name changed from "Soprano" to "Default" (it is the
label the UI shows; its id stays `default`, and `GET /v1/voices` on a
Soprano service shows the new name too), and the language display names
(`LANGUAGE_NAMES`) used in the labels are new.

## Alternatives considered

- **Loopback HTTP on the services.** Reachable by other local users and web
  pages, changes with `bind`, and dies with the service it should restart.
- **A separate control daemon.** One more LaunchDaemon and one more root
  install step for what the watchdog already is.
- **A line editor over `tomllib` instead of `tomlkit`.** No new dependency,
  but it only handles plain `[section]` tables, needs a lossy fallback for
  everything else, and was more code than the dependency it avoids.
- **Engine catalogues in the watchdog process.** Simpler and faster, but it
  runs every installed plugin's import-time code inside the supervisor.
- **The browser form without a key on loopback.** Convenient, but DNS
  rebinding makes loopback reachable from any page the operator visits.
- **Restart on `settings.set`.** A change that needs a model reload should
  not happen as a side effect; the UI can ask for it with `service.restart`.

## Consequences

- No root and nothing on a network interface. Nothing here listens on TCP.
- `tomlkit` is a new runtime dependency.
- The socket path must fit a Unix socket path (about 100 bytes), another
  reason to keep `venv_dir` short; moving the venv moves the socket.
- The state directory becomes 0700. It is the user's own service
  environment, so nothing else needs to read it.
- `engines.list` and engine changes cost a child process (a fraction of a
  second, then cached for a minute).
- `voiced status` gains state, uptime and restart counts when the watchdog
  answers; `voiced doctor` checks that the socket exists, is 0600, belongs
  to the user and answers `status`.

## Amendment (ADR 0017)

The state directory is `[install] venv_dir`, but it is no longer the venv:
it holds the entry point link `bin/voiced`, the environments voiced built,
`install.json` and the socket, and the environment may live elsewhere
(Homebrew's keg). Moving the state directory still moves the socket. The
engine catalogue child process runs the interpreter of the environment
`bin/voiced` resolves to, not the watchdog's `sys.executable`, which an
upgrade may have deleted.
