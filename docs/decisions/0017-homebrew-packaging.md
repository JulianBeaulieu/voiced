# 0017: Homebrew packaging

Date: 2026-10-15. Status: accepted.

## Context

After first install, voiced should not need a terminal, and the first
install should be one `brew install` plus one setup command. Three things
make Homebrew harder here than for a typical CLI:

- The services must start at boot, before anyone logs in, as the installing
  user and never as root (ADR 0004, ADR 0016 finding 9), with the watchdog
  restarting them by label (ADR 0012) and the models in that user's Hugging
  Face cache.
- The engines need MLX, `parakeet-mlx` and `mlx-audio`, which ship as
  binary wheels for Apple Silicon only. The `stt` and `tts` extras resolve
  to 88 packages in `uv.lock`, 265 compiled extension modules among them.
- Nothing is tagged yet, and `voiced` is not on PyPI, so `voiced install`
  outside a checkout has needed `--source` (ADR 0016 finding 10).

`~/.voiced` is also more than the venv: it is the state directory that holds
the control socket (ADR 0013), and the menu bar app looks for the socket
there without reading the config (ADR 0014).

## Decision

**Homebrew installs the program; `voiced install` installs the services.**

The formula (`packaging/homebrew/voiced.rb`, copied to the tap
`JulianBeaulieu/homebrew-voiced` after review) builds a venv in the keg's
`libexec` from Homebrew's `python@3.12`, installs every dependency at the
version and hash in `uv.lock` (`uv export --frozen`, then
`uv pip install --require-hashes --no-deps`, wheels from PyPI), installs
voiced itself without dependencies, and links `voiced` into `bin`. It
depends on `espeak-ng` and `ffmpeg`, `arm64` and macOS 14; `uv` is a build
dependency only. There is a `head` stanza against `main`; the `url` and
`sha256` lines for the first tag are written out in a comment, to be filled
in when v0.1.0 is tagged. Its `caveats` print the two setup commands.

When `voiced` runs from a keg (`sys.prefix` is
`<prefix>/Cellar/voiced/<version>/libexec`), `voiced install`:

- needs no `--source` and builds no venv. It writes two launchers into
  `~/.voiced/bin`, `voiced` and `python`, each a `/bin/sh` script that
  `exec`s the same program in `<prefix>/opt/voiced/libexec/bin`, plus a
  `~/.voiced/.homebrew` marker;
- downloads the models and stages the plists and the sudo script exactly as
  before. The plists still run `~/.voiced/bin/voiced`, so they are byte for
  byte those of a source install;
- refuses, before writing anything, when `~/.voiced` is a venv from a source
  install, and says how to switch (remove it, or pass `--source` to keep
  building from source). A source install likewise refuses a `~/.voiced`
  that has the marker.

The launchers point at `opt`, which Homebrew repoints at the current keg on
every upgrade, so the daemons always find a working environment without a
new root step. They are scripts, not symlinks: Python finds its venv from
the path it was started as, and `~/.voiced/bin/python` linked to the keg's
python started the bare `python@3.12` interpreter (`sys.prefix` was the
framework, `import mlx` failed). `exec` keeps the pid launchd and the
watchdog see. `voiced uninstall` deletes a `~/.voiced` with the marker the
way it deletes one with `pyvenv.cfg`, never touches the keg, and says to
`brew uninstall voiced` afterwards.

`voiced doctor` decides whether a port's listener is voiced from its
command line (`ps -o args=`) instead of its executable, because Homebrew's
Python is a framework build: every brewed voiced process has the executable
`.../Python.framework/.../Python.app/Contents/MacOS/Python`, and only the
argument `/opt/homebrew/opt/voiced/libexec/bin/voiced serve tts` names the
environment.

### Measured

On an M1 Max, macOS 15.7, Homebrew 7.0.9, from a local tap
(`brew tap-new`, formula copied in):

| Step | Result |
|---|---|
| `brew install --HEAD ./packaging/homebrew/voiced.rb` | refused: Homebrew 7 installs formulae only from a tap, so until the tap is published a local one stands in (`brew tap-new --no-git`, copy the file into its `Formula/`) |
| `brew install --HEAD --build-from-source` | 42 to 51 s for the voiced build over four builds, 59 to 75 s wall clock with the dependencies poured; the first run also upgraded outdated `ffmpeg`, `openssl@3`, `readline` and `sqlite`, as any `brew install` does |
| Keg size | 830 MB, 20,786 files, of which about 71 MB bytecode compiled at install time so the daemons never write into the keg; `python@3.12` adds 69 MB. A source install's `~/.voiced` is 677 MB |
| `brew test voiced` | passes: `voiced --version`, and `import mlx.core, parakeet_mlx, mlx_audio` |
| MLX from the keg | `Device(gpu, 0)`; Homebrew's relocation of the 265 extension modules did not break them |
| `brew audit --strict --online`, `brew style` | no problems |
| `voiced install` (models already cached) | 12.9 s: two launchers, the marker, both engines loaded once, plists and scripts staged. With an existing source venv in `~/.voiced`: refused with the switch instructions, nothing written |
| `voiced serve tts` started as the plist starts it | `/health` ok, `POST /v1/audio/speech` 200; the pid launchd would see is the Python process; `voiced doctor`: "Port 8880 in use by voiced" |
| `brew upgrade --fetch-HEAD voiced` with that server running | 78 s; `opt/voiced` repointed to the new keg and the old keg deleted in the same command. The running server kept answering `/health` (200) from the deleted keg; `~/.voiced/bin/voiced --version` ran the new one. The old keg had 62 more files than the new: `.pyc` files the server had written for voiced's own package, which is now byte-compiled at install as well |
| `brew uninstall voiced` | removes the keg, and with it the `python@3.12` and `uv` that only voiced needed |

## Alternatives considered

**`brew services`.** The service block below, rendered with
`Formula#service.to_plist`, gives:

```xml
<key>Label</key>              <string>sh.brew.voiced</string>
<key>KeepAlive</key>          <true/>
<key>RunAtLoad</key>          <true/>
<key>LimitLoadToSessionType</key>
<array><string>Aqua</string><string>Background</string><string>LoginWindow</string>
       <string>StandardIO</string><string>System</string></array>
<key>ProgramArguments</key>
<array><string>/opt/homebrew/opt/voiced/bin/voiced</string><string>serve</string><string>stt</string></array>
<key>StandardOutPath</key>    <string>/opt/homebrew/var/log/voiced-stt.log</string>
```

Without sudo it goes to `~/Library/LaunchAgents` and starts at GUI login.
With `sudo brew services start --sudo-service-user=<user>` it goes to
`/Library/LaunchDaemons` with `UserName` set, which would meet the boot
requirement, but: a formula has one service and voiced needs three
(the watchdog restarts `stt` and `tts` by their own labels); the label is
`sh.brew.voiced`, not the ADR 0003 prefix; the plist has no `HOME`,
`VOICED_CONFIG`, `HF_HOME`, espeak-ng or `PATH` environment, which
`voiced install` resolves per user; and it means running `brew` itself as
root. Rejected.

**`virtualenv_install_with_resources`.** Homebrew's standard Python path:
one `resource` block per dependency, 88 here, each built from its source
distribution (`pip install --no-binary :all:`). MLX from source needs CMake
and the Metal toolchain and takes far longer than the build above; `llvmlite`
needs LLVM. `brew update-python-resources` would regenerate the blocks, but
from sdists, not the wheels the project is tested with. Wheel URLs in
resource blocks work in a tap, but duplicate `uv.lock` by hand and drift
from it. Rejected.

**pipx style** (`pip install voiced[stt,tts]` into `libexec` with no lock).
Not reproducible: the same formula would install different dependency
versions on different days, and voiced pins `mlx-audio` narrowly because it
relies on its internals. It also needs voiced on PyPI. Rejected.

**A second venv for the daemons.** Keep `voiced install` building
`~/.voiced` as today and use Homebrew only for the CLI. Every install holds
two copies of the environment (830 MB plus 677 MB), `brew upgrade` changes
the CLI but not the daemons, so the control channel client and server can
differ in version, and `voiced install` would still need a source to build
from. Rejected.

**Pointing the plists at `<prefix>/opt/voiced/libexec/bin/voiced` and the
state directory into the keg.** One fewer indirection, but the control
socket would live inside the keg, the menu bar app would no longer find it
at `~/.voiced/control.sock`, and every check that reads `venv_dir` would
need to know about two places. Rejected for the launchers above.

## Consequences

- First install from the formula is
  `brew install --HEAD julianbeaulieu/voiced/voiced` (until the first tag),
  `voiced install`, and the printed sudo step. The end-user path will be the
  cask below.
- Upgrading is `brew upgrade voiced` and `voiced restart all`. No root step,
  because the plists do not change. A release whose plists do change says so
  in the changelog; then `voiced install` and the sudo step again.
- During `brew upgrade` the running daemons keep the old keg's files open
  until they restart; Homebrew's cleanup deletes the old keg. A daemon that
  exits for any reason restarts through the launcher into the new keg.
  `voiced restart all` makes that immediate.
- `brew uninstall voiced` while the daemons are installed leaves launchd
  restarting a launcher whose target is gone, every 10 s. The caveats say to
  run `voiced uninstall` and its sudo step first.
- The formula takes its dependency versions from `uv.lock` at the tagged
  commit; there is nothing to keep in sync by hand. `brew upgrade` of
  `python@3.12` within 3.12 does not break the keg, since Homebrew's
  `virtualenv_create` points the venv at `opt/python@3.12`.
- `PUBLISHED` still gates name-based installs from PyPI; Homebrew does not
  need it.

### The end-user path: a cask for the menu bar app

The formula is the headless and advanced path: a Mac with no one at the
screen, a terminal user, or scripting. Once the signed app exists, the
recommended Homebrew path for everyone else is a cask in the same tap that
installs the menu bar app, and the app installs the daemons itself; that install flow is designed separately in
ADR 0018 and not here. What this record fixes for the cask: `voiced.app`
comes from a GitHub release asset, a Developer ID signed, notarized and
stapled zip built by CI as `menubar/Scripts/SIGNING.md` describes; the
cask waits for that signing identity, because Gatekeeper quarantines an
unsigned app on first open. Whatever the app installs should keep
`~/.voiced` as the state directory, where it already looks for the control
socket (ADR 0014), so a machine set up with the formula and one set up by
the app look the same to the daemons, the CLI and the app.
