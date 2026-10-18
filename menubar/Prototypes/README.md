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

`voiced-privileged.sh`, the one script the app would run as root behind one
administrator prompt, with two modes:

```
voiced-privileged.sh install   USER STT_B64 TTS_B64 WATCHDOG_B64
voiced-privileged.sh uninstall USER
```

The plists arrive base64-encoded, are checked against a key allowlist and
exact values (the program is always `~/.voiced/bin/voiced`), and are rebuilt from the checked values before anything is
bootstrapped. In the app the script's text is compiled into the signed
executable and run with `env -i` (ADR 0018 has the details). Run as a normal
user it needs `VOICED_TEST_DEST` (a scratch directory standing in for
`/Library/LaunchDaemons`) and skips launchctl and the firewall; as root that
variable is ignored.

`test.sh` runs it that way against the plists `voiced install` staged for
the default `~/.voiced`, and against crafted variants that must all be
refused with nothing written:

```sh
menubar/Prototypes/PrivilegedInstall/test.sh ~/.config/voiced/launchd
```
