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
kickstart`). It serves `<state dir>/control.sock`; the state dir is
`[install] venv_dir` (`~/.voiced` by default). The watchdog loop moved onto
asyncio (`Watchdog.run_async`): each tick still runs the same blocking code,
in a background thread, and a lock keeps a tick and a requested restart from
interleaving. A requested restart goes through `Watchdog.restart_now`, the
same code a watchdog decision uses, so it counts towards `restarts`, starts
the startup grace period and is not followed by an automatic restart while
the model loads. The cooldown does not hold back a requested restart.

The `voiced.control` package imports only the standard library,
`voiced.config` (pydantic) and the watchdog; the engine registry is imported
only for `engines.list` and the engine checks of `settings.set`. The
watchdog stays importable without FastAPI. If the socket cannot start
(another watchdog answers on it, the state directory belongs to someone
else, the path is too long for a Unix socket), the error is logged and the
watchdog runs without it: supervising the services matters more.

When nothing answers on the socket, clients treat that as "voiced is not
running or not installed". The CLI then falls back: `voiced restart` runs
the same SIGKILL-and-kickstart itself, `voiced settings` reads and writes the
config file directly with the same function, and `voiced status` shows
launchd and `/health` only.

### A Unix socket, not loopback HTTP

Loopback HTTP is reachable by every local user and by every web page the
user opens (DNS rebinding, `fetch` to `127.0.0.1`), and it changes meaning
when someone sets `bind = "0.0.0.0"`. A Unix socket has a filesystem
permission and a kernel-reported peer:

- the state directory is created (or tightened) to 0700, and the socket is
  0600; a directory owned by another uid is refused;
- every connection's peer uid (`LOCAL_PEERCRED` on macOS, `SO_PEERCRED` on
  Linux) must equal the server's uid; anything else, including a platform
  that cannot say, is closed before the `hello` is sent;
- a socket file nobody answers on is a leftover from a crash and is removed
  at start; one that answers belongs to a running watchdog and is left
  alone; a file that is not a socket is never deleted.

The framing is newline-delimited JSON with request ids and server-pushed
events, so a Swift client needs nothing beyond Foundation and a shell user
can try it with `nc -U`. Lines are limited to 64 KiB.

### What is settable

`bind` (`loopback` or `lan`), `api_key`, `zeroconf`, `browser_settings`,
`stt.engine`, `stt.model`, `tts.engine`, `tts.model`, `tts.voice`
(`[tts] default_voice`) and `tts.speed`. Ports, log locations, the label
prefix, the watchdog policy and the install keys are not: changing them
needs the plists re-staged or the daemons re-installed, which is a root
step. `bind = "lan"` writes `0.0.0.0`, unless the file already has a
specific non-loopback address, which is kept.

`voiced.config.apply_settings` is the only writer, for the socket, the CLI
fallback and the status page form:

- the would-be file contents are validated with the `Settings` section
  models, without environment overrides (an invalid file value must not
  hide behind a variable), plus a check that an engine being selected is
  installed and that the voice exists in its catalogue; an invalid value
  never reaches the file;
- the file is changed line by line: only the lines for changed keys are
  rewritten, so comments, key order and every other key stay as they were.
  The tree has `tomllib` (read only) and no TOML writer, so this is a small
  purpose-built editor for plain `[section]` tables of scalar keys. If the
  file uses another layout (dotted keys, inline tables, multi-line values)
  or the edit does not parse back to exactly the intended data, the file is
  rewritten from the parsed data instead and its comments are lost; the
  documented copy is still `config.example.toml`;
- it is written to a temporary file in the same directory, fsynced, given
  the old file's mode (0600 for a new file, which may hold the key) and
  renamed over the old one;
- `api_key` is written but never read back out: `settings.get` reports
  `api_key_set` only.

Engine catalogues come from an optional `catalogue()` classmethod on the
engine class (`voiced.engines.catalogue`): Kokoro returns its static voice
table, Soprano its one voice. It is not part of the frozen interface in
`voiced.engines.base`; engines without it are described by `model_id` and
`languages`, and the running service's `voices()` remains the authority.

### Restart rules

`settings.set` never restarts anything. It returns `restart_required`,
derived from which keys actually changed value:

| Changed | Restart |
|---|---|
| `[server]` keys: `bind`, `api_key`, `zeroconf` | `stt`, `tts` |
| `stt.*` | `stt` |
| `tts.*` | `tts` |
| `browser_settings` | none: the status page reads it from the file on each request |

A value equal to the current one changes nothing and needs no restart.
Warnings: `lan_without_api_key` when `bind` or `api_key` was part of the
change and the result is a LAN bind with no key, and `env_override:<key>`
when a `VOICED_*` variable shadows the key just written. The watchdog
refreshes its own service specs (probe API key) after a change.

### The browser form

The status page can show a settings form, off by default
(`[server] browser_settings = false`). It calls the same function as the
socket. Rules, enforced on the server:

- off: `GET /settings.json` and `POST /settings` answer 404 with how to turn
  it on; the switch is read from the file on every request, so turning it
  off takes effect at once;
- a LAN bind and no API key: refused (403) with an explanation, whether the
  LAN bind is in the file or in what the running process was started with;
- an API key set: required as a bearer token; while a key change waits for
  a restart, either the old or the new key is accepted;
- `POST` must be `application/json`, and a request whose `Origin` does not
  match `Host` is refused, so another web page cannot submit it.

### Protocol version policy

The `hello` event carries `protocol: 1`. Adding operations, result fields,
event types, error codes or warnings is compatible: clients ignore unknown
fields and events. Removing or renaming anything, or changing a type or a
meaning, increments the version, and the server then speaks only the new
one. A client that sees a version it does not know should say so instead of
guessing. Additions in this first version beyond the contract: `detail` in
the `service.restart` result, `path` in `logs.tail`, `languages` on TTS
engines in `engines.list`, an optional `service` for `open status_page`,
`field` in `invalid_setting` errors and the `env_override:<key>` warning.

## Alternatives considered

- **Loopback HTTP on the services.** Reachable by other local users and web
  pages, changes with `bind`, and dies with the service it should restart.
- **A separate control daemon.** One more LaunchDaemon and one more root
  install step for what the watchdog already is.
- **`tomlkit` to keep comments.** A new runtime dependency for ten scalar
  keys; the line editor plus a verified fallback covers the files voiced
  writes and documents.
- **Restart on `settings.set`.** A change that needs a model reload should
  not happen as a side effect; the UI can ask for it with `service.restart`.

## Consequences

- No root and nothing on a network interface. Nothing here listens on TCP.
- The socket path must fit a Unix socket path (about 100 bytes), another
  reason to keep `venv_dir` short.
- The state directory becomes 0700. It is the user's own service
  environment, so nothing else needs to read it.
- `voiced status` gains state, uptime and restart counts when the watchdog
  answers; `voiced doctor` checks that the socket exists, is 0600, belongs
  to the user and answers `status`.
