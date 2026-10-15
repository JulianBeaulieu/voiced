# Prototypes

Throwaway code behind [ADR 0018](../../docs/decisions/0018-install-from-the-menu-bar-app.md).
Nothing here is part of the app or its package: SwiftPM does not see this
directory, and CI does not build it.

## SMDaemonProbe

A bundle that registers an `SMAppService` LaunchDaemon whose `BundleProgram`
is a launcher inside the bundle, which records its uid and execs a program
outside the bundle (`/usr/bin/id`). The plist names the current user as
`UserName`.

```sh
out="$(mktemp -d)"
menubar/Prototypes/SMDaemonProbe/build.sh "$out" -            # or a codesigning identity
"$out/SMDaemonProbe.app/Contents/MacOS/SMDaemonProbe" register   # status, settings, unregister
```

`register` leaves an entry in System Settings > Login Items; always finish
with `unregister`. Approving it there (an administrator is asked) lets the
job run once and append a line to `$out/probe.out`. ADR 0018 has the results.

## PrivilegedInstall

`install-daemons.sh`, the script the app would run as root behind one
administrator prompt. It copies the staged plists, checks the root-owned copies
and only then bootstraps them. To try it without root, point it at a scratch
directory and skip launchctl:

```sh
VOICED_DEST="$(mktemp -d)" VOICED_NO_LAUNCHCTL=1 \
  menubar/Prototypes/PrivilegedInstall/install-daemons.sh "$(id -un)" \
  ~/.config/voiced/launchd io.github.julianbeaulieu.voiced.stt \
  io.github.julianbeaulieu.voiced.tts io.github.julianbeaulieu.voiced.watchdog
```
