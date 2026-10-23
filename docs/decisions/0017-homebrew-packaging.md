# 0017: Homebrew packaging and the shared install layout

Date: 2026-10-15, revised 2026-10-18, 2026-10-20 and 2026-10-21. Status: accepted.

## Context

After first install, voiced should not need a terminal, and the first
install should be one `brew install` plus one setup command. Three things
make Homebrew harder here than for a typical CLI:

- The services must start at boot, before anyone logs in, as the installing
  user and never as root (ADR 0004, ADR 0016 finding 9), with the watchdog
  restarting them by label (ADR 0012) and the models in that user's Hugging
  Face cache.
- The engines need MLX, `parakeet-mlx` and `mlx-audio`, which ship as
  binary wheels for Apple Silicon only. The `stt` and `tts` extras lock 88
  packages in `uv.lock`, 82 of them for Python 3.12 on macOS; installed,
  they hold 243 compiled extension modules.
- Nothing is tagged yet, and `voiced` is not on PyPI.

There will be three ways to install: this formula, a source checkout, and
the menu bar app (ADR 0018). Before this record `~/.voiced` was both the
venv a source install built and the state directory that holds the control
socket (ADR 0013), where the menu bar app looks without reading the config
(ADR 0014). Homebrew's environment lives in the keg, so the two roles have
to come apart, the same way for every path.

## Decision

**Homebrew installs the program; `voiced install` installs the services.**
All three install paths share one state directory layout, below.

### The install layout

These rules hold for every way of installing voiced: `voiced install` from
Homebrew or from any other environment, `voiced install --source`, and the
menu bar app. `voiced.layout` implements them.

**The state directory** is `[install] venv_dir`, `~/.voiced` by default
(the key keeps its name; it no longer names a venv):

```
~/.voiced/
  bin/voiced      symlink to the active environment's bin/voiced; the plists run this
  envs/<id>/      environments built by `voiced install --source` (or the app)
  python/         uv-managed Pythons those environments run on
  control.sock    the watchdog's control socket (ADR 0013)
  stt.off         the pid of a service switched off and parked (ADR 0033); tts.off too
  install.json    the manifest
```

**The manifest**, `install.json`, says that the directory is voiced's and
which environment is active:

```json
{"source": "homebrew", "environment": "/opt/homebrew/opt/voiced/libexec",
 "version": "0.0.1", "lock_sha256": "…", "python": "/opt/homebrew/Cellar/python@3.12/…/python3.12"}
```

`source` is `cli`, `homebrew` or `app`; `environment` the directory linked
(null while a first install is in progress); `version` voiced's version in
it; `lock_sha256` the SHA-256 of the dependency lock it was installed from;
`python` its interpreter, resolved; `previous` the environment that was
active before it (null for none), which pruning keeps and the menu bar
app rolls back to (ADR 0018). Re-running install in the active
environment keeps `previous`; switching away from a legacy venv leaves it
null, since that venv is removed.

**Adopt or refuse.** One rule, `voiced.layout.claim`, decides for install
and uninstall alike. A state directory is voiced's when it does not exist,
is empty, holds only `control.sock` (or `.DS_Store`), has `install.json`,
or is a legacy venv: `pyvenv.cfg` at its top *and* voiced installed in it,
not editable (`lib/python3*/site-packages/voiced-*.dist-info` without
`"editable": true` in its `direct_url.json`), an install from before this
record, adopted as `environment = ~/.voiced`. `voiced install` always made
a regular install; a checkout's `.venv` from `uv sync` has an editable
one, so passing it as `--state-dir` is refused. A venv of the user's own
with a regular install of voiced is indistinguishable from a legacy one
and would be adopted, but only if named explicitly. Anything else, a user's
own venv included, is refused, naming the files, before the config or
anything else is written, and uninstall leaves it alone. A fresh directory
is claimed by writing the manifest first, with `environment: null`, so an
interrupted install is still recognised. The app (ADR 0018) does the same
before it builds `python/` and `envs/` there, so the directory it hands to
`voiced install` is already claimed. voiced never recognises an unclaimed
directory by what is in it. A state directory that is itself a symlink is
refused, whatever it points at: uninstall would delete inside the target.

**One install at a time.** `voiced install` (all of it) and `voiced
uninstall --yes` hold an exclusive `flock` on the state directory itself
(no lock file, which in a refused directory would be litter); a second one
says it is waiting and waits. The app takes the same lock. Without it, the
app and the CLI could prune each other's new environment, leaving
`bin/voiced` dangling and every job stopped by `PathState`, or build into
the same `envs/<timestamp>`. Environments still being built
(`envs/<id>.partial`, ADR 0018) are never pruned; uninstall, which holds
the lock the builder holds, removes them, so an interrupted build does not
keep `~/.voiced` forever.

**Environment names (2026-10-20).** The lock alone does not keep two
installs out of one `envs/<id>`: `voiced install --source` picks the name
when it plans, before it waits for the lock. An `<id>` is a UTC timestamp
(`20261020T120000`), with `-2`, `-3` appended when that name is taken.
Taken means the directory, its `.partial` or its `.broken` exists, or
`install.json` names it as `environment` or `previous`, even if it is gone.
So `previous` never equals the new environment: the install records only
one of the manifest's two named paths there, and the new name avoids both.
A CLI install that waited for the lock refuses to build when another
install took its name meanwhile, and says to run it again. The app applies
the same rule when it picks a name, which it does under its lock.

**One install mode.** Without `--source`, the environment is `sys.prefix`
of the running `voiced`, and `voiced install` links `bin/voiced` to its
`bin/voiced`. A Homebrew keg path, `<prefix>/Cellar/voiced/<version>/libexec`,
is linked as `<prefix>/opt/voiced/libexec`, which Homebrew repoints at the
current keg on every upgrade; that mapping is the only Homebrew-specific
code. With `--source <checkout>`, `voiced install` builds a new environment
into `envs/<UTC timestamp>` on a uv-managed Python in `python/`
(`UV_PYTHON_INSTALL_DIR`; `uv python uninstall` cannot break it) and links
that. A build never writes into an environment a daemon may be running.
The environment must import the engines (`mlx.core`, `parakeet_mlx`,
`mlx_audio`): a `uv tool install` of voiced has no extras and is refused
with a pointer to `--source`; that check also reports voiced's version for
the manifest. Then the models are downloaded, and only then does anything
the daemons use change: the link (replaced atomically, by rename), the
manifest, a legacy venv's removal and pruning. A failed install leaves the
previous one as it was. The app runs `<its environment>/bin/voiced install
--origin app`; `--origin` only sets the manifest's `source`, which
otherwise is `homebrew` for a keg and `cli` for anything else. Running
`voiced install` again from the active environment keeps the manifest's
`source` and prunes nothing; `--source` always builds a new environment,
which the watchdog then moves the services to.

**Switching** between Homebrew, a source build and the app is `voiced
install` from the other one, which repoints `bin/voiced`, then `voiced
restart all` (or one watchdog poll, below). Never root, never deleting the
state directory. `install` keeps the newly linked environment and the
previous one in `envs/` and removes older ones there; it never removes an
environment outside `envs/`. The first switch away from a legacy venv has
no rollback: its `bin/voiced` is the entry point path, so the two cannot
coexist, and once the new environment has been verified (the engine check)
and linked, the venv's files (`lib`, `include`, `share`, `pyvenv.cfg` and
the rest of `bin`) are removed; anything else in the directory stays.

**The plists are identical for every source**: they run `~/.voiced/bin/voiced`,
with `KeepAlive` set to exactly `{PathState: {<home>/.voiced/bin/voiced: true}}`.
launchd follows the symlink: while it resolves, a service that exits is
restarted as with `KeepAlive: true`; once it dangles (`brew uninstall
voiced` with the daemons installed, or an environment deleted by hand),
launchd stops respawning a program that cannot start and resumes when the
target is back. Measured in the user domain with a test job: six starts in
12 s while the link resolved, none in 15 s while it dangled, restarts again
once restored.

**One lock for all paths.** `uv export --frozen --no-dev --extra stt
--extra tts --no-emit-project --no-header` is shipped as package data,
`voiced/requirements.lock`, and the build backend likewise,
`voiced/build-requirements.lock` (a `build` dependency group pins
`hatchling`, so it is in `uv.lock` with hashes). A test fails when either
differs from `uv.lock`, so CI catches a stale one, and names the one command
that rewrites both and NOTICE, `scripts/refresh_locks.py`. Every path runs
`uv pip install --require-hashes --no-deps -r requirements.lock`, then
`uv pip install --no-deps --build-constraints build-requirements.lock
<voiced>`; uv verifies the hashes of build dependencies in constraints.
`install`, not `pip sync`, which would remove third-party engine plugins
(ADR 0002, ADR 0013). The lock's SHA-256 goes in the manifest, and
`voiced doctor` warns when the environment's lock no longer matches it.
The build backend stays a lock of its own, not folded into the runtime one
with `--no-build-isolation`: it is only needed to build voiced, and in the
runtime lock `hatchling` and its dependencies would be installed into
every environment, the keg included, for nothing.

**Dependabot's pull requests (2026-10-21).** They change only
`pyproject.toml` and `uv.lock`, so the shipped locks and NOTICE disagree
with `uv.lock` and CI fails. `.github/workflows/dependency-locks.yml` runs
`scripts/refresh_locks.py` on Dependabot's `uv` branches and pushes the
result, with no repository secret. A Dependabot-triggered `pull_request` run
gets a read-only token by default, which the workflow's `permissions` key
may raise; a push with `GITHUB_TOKEN` that updates a pull request starts its
CI in an approval-required state, which one click from someone with write
access releases (GitHub's "Triggering a workflow" documentation). The job
that regenerates runs the branch's code (`uv lock` may build an sdist), so
it holds a read-only token; a second job, on another runner and running
nothing from the branch, copies the generated files over its checkout,
commits and pushes, not forced. The commit carries `[dependabot skip]`, so
Dependabot keeps rebasing the branch, and the workflow runs again on each
rebase. Not yet exercised on a real Dependabot pull request: the first
`uv` one after this change is its test.

**The watchdog follows a replaced environment.** At start it records the
files it runs from, `sys.prefix`, `sys.base_prefix` and `sys.executable`,
resolved (Homebrew's venv reaches Python through `opt/python@3.12`, which
a patch upgrade repoints), with their inodes, and the link
`~/.voiced/bin/voiced` (`readlink`, one level; a legacy venv's plain
script counts as no link). Before every poll it checks two things:

- *Files vanished or were replaced*: `brew cleanup` deleted the old keg
  (by default at the end of `brew upgrade`), `brew reinstall` built a new
  keg at the same path (Homebrew renames the old one to `<keg>.reinstall`
  and deletes it at the end; same path, new inode), a python@3.12 upgrade
  deleted the old interpreter, or `voiced install` pruned the old
  `envs/<id>`.
- *The entry point was repointed*: `voiced install` and ADR 0018's switch
  and swap-based update. For a Homebrew install the link is always
  `opt/voiced/libexec/bin/voiced`, so `brew upgrade` never triggers this
  one; a Homebrew upgrade is caught by the first, when the old keg goes.

Either way it restarts both services and exits, and KeepAlive starts it
again from the new environment.

**Nothing restarts while Homebrew works on the environment.** Before every
poll, and before a restart asked for over the control socket (`voiced
restart`, the app), the watchdog checks whether Homebrew holds the lock of
a formula its files belong to (`<prefix>/var/homebrew/locks/<formula>.formula.lock`,
an exclusive `flock` for the whole install, upgrade or reinstall; the
watchdog tries a shared one and lets go at once). While it does, the poll
is skipped (no health checks, no health restarts, no move to a new
environment) and a requested restart is refused with that reason before
the control op answers (`service_unavailable`: `voiced restart` exits
non-zero, the app is told). This covers all of `brew upgrade`, during
which nothing else says anything has changed: the old keg stays until
cleanup, at the end. A hold longer than 30 minutes is taken for a hung or
suspended `brew`, and the watchdog restarts as usual again; the wait is
logged when it starts, every 5 minutes, and when it ends. The reason is
measured: Homebrew repoints `opt/voiced` at the new keg (`FormulaInstaller#finish`,
`link`) before it rewrites and ad-hoc re-signs the keg's Mach-O files in
place (`fix_dynamic_linkage`; 7 of the 243 extension modules, the Rust
ones that are `MH_DYLIB`s among them). A watchdog that followed the
repointed `opt` link mapped `_pydantic_core` from the new keg 3 s before
Homebrew rewrote it, and from then on every process loading that file was
killed (`OS_REASON_CODESIGNING`) or hung in `dlopen` in the kernel,
unkillable, until the file was replaced by a copy. So the resolved path of
`bin/voiced` is deliberately not compared, and nothing restarts under a
held lock. With `HOMEBREW_NO_INSTALL_CLEANUP` set, `brew upgrade` deletes
nothing: the daemons stay on the old keg, which is intact, until
`brew cleanup` or `voiced restart all`.

What remains: a service that crashes during the seconds between Homebrew
repointing `opt` and re-signing the keg is restarted by launchd's
KeepAlive, which the watchdog cannot hold back, on files about to be
rewritten, and can leave them unloadable. Recovery is
`brew reinstall voiced`, which writes every file anew.

A running process whose keg is gone still answers `/health` from memory,
but `subprocess(sys.executable)` fails, lazy imports fail (`scipy.signal`;
with the stdlib gone, `No module named 'xml'`), and so does the engine
catalogue; that is why it is not left running. The catalogue child
process runs the interpreter next to what `bin/voiced` resolves to, looked
up on every call, not the watchdog's own `sys.executable`.

**Uninstall removes what the layout owns**, in a directory `claim`
accepts: `bin/voiced`, the built environments in `envs/` (with a
`pyvenv.cfg`, not `.partial`), `python/`, `control.sock`, `install.json`,
a legacy venv's files, then `bin`, `envs` and the state directory each
only if empty. Anything else stays, and so does the state directory
holding it; an environment outside the state directory (Homebrew's keg,
any other) is never touched. A directory `claim` refuses is left alone.

**PyPI is not involved.** Neither Homebrew nor `voiced install` installs
voiced by name from PyPI: `voiced install` links the environment it runs
from or builds one from a checkout. The `PUBLISHED` switch that guarded
name-based installs is gone; a later PyPI release would be one more
environment to run `voiced install` from.

### The formula

The formula (`packaging/homebrew/voiced.rb`, copied to the tap
`JulianBeaulieu/homebrew-voiced` after review) builds a venv in the keg's
`libexec` from Homebrew's `python@3.12`, installs the shipped lock and then
voiced as above with `--compile-bytecode` (so the daemons never write into
the keg) and the uv cache in `HOMEBREW_CACHE/uv` (no 300 MB download per
build), and links `voiced` into `bin`. It depends on `espeak-ng` and
`ffmpeg`, `arm64` and macOS 14; `uv` is a build dependency only. There is
a `head` stanza against `main`; the `url` and `sha256` lines for the first
tag are written out in a comment, to be filled in when v0.1.0 is tagged
with `version = "0.1.0"` in `pyproject.toml`. Its `caveats` print the setup
commands and the upgrade and uninstall order.

`voiced doctor` decides whether a port's listener is voiced's daemon from
its argv[1]: the plist runs the console script `~/.voiced/bin/voiced`, so
the kernel starts `<interpreter> ~/.voiced/bin/voiced serve <service>`.
argv[0] varies, and for Homebrew's framework Python the executable is
`.../Python.app/Contents/MacOS/Python` wherever the venv is.

### Measured

On an M1 Max, macOS 15.7, Homebrew 7.0.9, from a local tap
(`brew tap-new`, formula copied in):

| Step | Result |
|---|---|
| `brew install --HEAD ./packaging/homebrew/voiced.rb` | refused: Homebrew 7 installs formulae only from a tap, so until the tap is published a local one stands in ([packaging/homebrew/README.md](../../packaging/homebrew/README.md)) |
| `brew install --HEAD --build-from-source` | 41 s with an empty uv cache (34 s build), pulling in `python@3.12` and `uv`; `brew upgrade --fetch-HEAD` 15 s with the cache in `$(brew --cache)/uv` |
| Keg size | 830 MB, 20,794 files, of which about 71 MB bytecode compiled at install time; `python@3.12` adds 69 MB |
| `brew test voiced` | passes: `voiced --version`, and `import mlx.core, parakeet_mlx, mlx_audio` |
| MLX from the keg | `Device(gpu, 0)`; Homebrew's relocation of the 243 extension modules did not break them |
| `brew audit --strict --online`, `brew style` | no problems |
| `voiced install` from the keg, scratch state directory | links `bin/voiced` to `opt/voiced/libexec/bin/voiced`, writes `install.json` (`source = homebrew`), stages the plists; refuses a state directory holding a foreign `precious.txt` |
| `voiced install --source`, scratch state directory | 7 s with a warm uv cache: 82 packages from the lock with `--require-hashes`, voiced built with the hashed build backend, Python 3.12.15 under `python/`; a second `voiced install` from the keg repoints the link to Homebrew's environment |
| `voiced uninstall --yes` | removes `bin/voiced`, `install.json`, `envs/*` and `python/`; with a foreign `precious.txt` in the state directory, keeps the directory and the file |
| The three plists as user-domain jobs (fake engines), then `brew upgrade --fetch-HEAD` | nothing while the old keg exists (`opt/voiced` already repointed); 3 s after `brew cleanup voiced` deleted it, the watchdog logged it, exited, and launchd started it again from the new keg 0.5 s later. Its service restarts found nothing, since it looks services up in the system domain; kickstarted by hand, they came up on the new keg and answered `/health`. The restart path itself is covered by unit tests |
| The same with the earlier rule, `realpath(bin/voiced)` | the watchdog restarted 4 s before `brew upgrade` returned, mapped the new keg's `_pydantic_core` 3 s before Homebrew rewrote it; afterwards every process loading that file was killed (`OS_REASON_CODESIGNING`) or hung in `dlopen`, and copies of the 7 rewritten files (new inodes) loaded again |
| `python@3.12`'s keg moved aside, then back | the watchdog noticed within one poll (5 s), exited, launchd retried with `EX_CONFIG` while Python was missing, and the watchdog was back 5 s after the keg was |
| `brew reinstall voiced` under running jobs, with the earlier rule (path existence) | Homebrew renamed the keg to `<keg>.reinstall`, built a new one at the same path and deleted the old one; `opt/voiced` never changed and the keg path always existed, so nothing noticed: the services kept running the deleted keg |
| The same with inodes and Homebrew's lock | the watchdog logged the replaced keg 4 s into the reinstall and waited; Homebrew re-signed the 7 modules 8 s later; the watchdog restarted when `brew reinstall` returned, and came back on the new keg; services kickstarted then loaded the rewritten modules |
| `brew upgrade --fetch-HEAD` under running jobs, polls held by Homebrew's lock | 3 s into the 15 s upgrade the watchdog logged that Homebrew was installing voiced and stopped polling; it resumed 2 s after the upgrade returned and moved to the new keg 3 s after `brew cleanup voiced` |
| A `uv venv` of the user's own as `--venv` | `voiced install` and `voiced uninstall --yes` both refused it; its contents were unchanged |
| `voiced doctor` after an upgrade the services were not restarted onto | each port warns that its listener runs another environment than `bin/voiced` links |
| `brew uninstall voiced` under running jobs | the watchdog noticed the deleted keg and exited; with `bin/voiced` dangling, launchd started none of the three jobs again in the following 60 s |

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
one `resource` block per dependency, 82 here, each built from its source
distribution (`pip install --no-binary :all:`). MLX from source needs CMake
and the Metal toolchain and takes far longer than the build above; `llvmlite`
needs LLVM. `brew update-python-resources` would regenerate the blocks, but
from sdists, not the wheels the project is tested with. Wheel URLs in
resource blocks work in a tap, but duplicate `uv.lock` by hand and drift
from it. Rejected.

**pipx style** (`pip install voiced[stt,tts]` into `libexec` with no lock).
Not reproducible: the same formula would install different dependency
versions on different days, and voiced pins `mlx-audio` narrowly because it
relies on its internals. The previous `voiced install` did this
(`uv pip install --upgrade`, re-resolving on every run, so source installs
drifted from `uv.lock`). It also needs voiced on PyPI. Rejected.

**A second venv for the daemons.** Keep `voiced install` building
`~/.voiced` as before and use Homebrew only for the CLI. Every install holds
two copies of the environment (830 MB plus 677 MB), `brew upgrade` changes
the CLI but not the daemons, so the control channel client and server can
differ in version. Rejected.

**Launcher scripts instead of a symlink** (the first version of this
record). `~/.voiced/bin/voiced` and `bin/python` as `/bin/sh` scripts that
`exec` the Homebrew environment, plus a `.homebrew` marker file. `voiced` is
a console script with an absolute shebang, so a symlink to it works;
only `python` needed a launcher, because Python finds its venv from the
path it was started as, and every user of `bin/python` can take
`realpath(bin/voiced).parent / "python"` instead. A script also hid the
entry point from `ps`, so `doctor` needed a Homebrew branch. Replaced by
the symlink and the manifest.

**Pointing the plists at `<prefix>/opt/voiced/libexec/bin/voiced` and the
state directory into the keg.** One fewer indirection, but the plists would
differ per install path (switching would need root), the control socket
would live inside the keg, and the menu bar app would no longer find it at
`~/.voiced/control.sock`. Rejected.

**A maintainer runs the command on every Dependabot pull request.** The
first version of this decision: check out the branch, run
`scripts/refresh_locks.py`, commit and push, once a month or more. It
rejected the workflow on two grounds that do not hold, that a Dependabot run
needs `pull_request_target` for a write token, and that a push with
`GITHUB_TOKEN` never starts CI, so a personal access token would be needed
(2026-10-21: replaced by the workflow above). Generating the locks at build
time instead is no way out: the formula and `voiced install --source` read
them from the source tree before anything is built, and NOTICE needs PyPI.
Dropping the `uv` ecosystem from Dependabot was the other; it proposes
updates and groups them, which is the useful part, so it stays.

## Consequences

- First install from the formula is
  `brew install --HEAD julianbeaulieu/voiced/voiced` (until the first tag),
  `voiced install`, and the `sudo` line it prints, which runs the
  privileged script of ADR 0018 from the keg's environment.
- Upgrading is `brew upgrade --fetch-HEAD voiced` until v0.1.0 (a plain
  `brew upgrade` is a no-op for a HEAD install), then `brew upgrade voiced`.
  No root step, because the plists do not change; the watchdog moves both
  services to the new keg once the upgrade has deleted the old one (with
  `HOMEBREW_NO_INSTALL_CLEANUP` set, at the next `brew cleanup`), or
  `voiced restart all` does it once `brew upgrade` has returned.
  A release whose plists do change says so in the changelog; then
  `voiced install` and the sudo step again.
- `brew uninstall voiced` while the daemons are installed leaves them
  loaded but stopped (PathState); `voiced doctor` says the entry point
  dangles. The caveats say to run `voiced uninstall` and its sudo step
  first. Homebrew does not show caveats at uninstall.
- The formula takes its dependency versions from the lock at the tagged
  commit, which a test keeps equal to `uv.lock`; nothing to keep in sync by
  hand. `brew upgrade` of `python@3.12` within 3.12 does not break the keg,
  since Homebrew's `virtualenv_create` points the venv at `opt/python@3.12`;
  running daemons are moved by the watchdog rule above once cleanup deletes
  the old interpreter.
- `depends_on "ffmpeg"` stays for now: `voiced doctor` and request decoding
  need `ffmpeg` on `PATH`. Once ADR 0018's pinned `imageio-ffmpeg` lands,
  drop it, and re-check whether `espeak-ng` is still needed the same way.
- A machine set up with the formula, from source or by the app looks the
  same to the daemons, the CLI and the menu bar app; only `install.json`
  and the link target differ.

### The end-user path: a cask for the menu bar app

The formula is the headless and terminal path: a Mac with no one at the
screen, a terminal user, or scripting. Once the signed app exists, the
recommended Homebrew path for everyone else is a cask in the same tap that
installs the menu bar app, and the app installs the daemons itself, into
the layout above; that install flow is ADR 0018. What this record fixes for
the cask: `voiced.app` comes from a GitHub release asset, a Developer ID
signed, notarized and stapled zip built by CI as
`menubar/Scripts/SIGNING.md` describes; the cask waits for that signing
identity and v0.1.0, because Gatekeeper quarantines an unsigned app on
first open.

## Amendment (2026-10-23)

The cask leaves the daemons to the app.

Up to 0.4.0 the cask's `uninstall` stanza stopped and removed the three
labels and deleted their plists (`launchctl:` and `delete:`). Homebrew runs
a cask's `uninstall` stanza not only for `brew uninstall` but also when it
upgrades or reinstalls the cask, including `brew install --cask` over an
older version; of its directives only `signal:` is skipped then
(`Cask::Artifact::AbstractUninstall::UPGRADE_REINSTALL_SKIP_DIRECTIVES`).
Both directives run with `sudo` for the system domain, `delete:` even when
the files are gone. So every update asked for a password in the terminal,
stopped and removed the services, and the app then found them missing and
asked for an administrator again to put them back: two prompts where ADR
0018 promises at most one.

The daemons were never the cask's: `voiced-privileged` installs them when
the app first opens. The cask now keeps only `quit:` in `uninstall`, and
`launchctl:` moves to `zap`, which Homebrew runs only for `brew uninstall
--zap` (and `brew reinstall --zap`); it stops and removes each label and
deletes its plist, so `delete:` is gone. An upgrade or reinstall asks
Homebrew for no password and leaves the services running; the app's
update then asks only when `voiced-privileged check` says the services
need it.

The trade-off: a plain `brew uninstall --cask voiced` removes the app and
leaves the services running from `~/.voiced`, as moving the app to the
Trash always has. The caveats name the ways to remove them: the app's
Uninstall before `brew uninstall`, `brew uninstall --zap --cask voiced`,
or afterwards the `voiced uninstall` steps in docs/operations.md.
Homebrew uses the stanza of the installed cask, not the new one, so the
first upgrade from 0.4.0 or earlier still runs the old stanza once.
