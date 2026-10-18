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

The privileged script that started here is now part of the app:
`Sources/VoicedMenuBarUI/Installer/PrivilegedScript.swift`, and its test is
`Scripts/test-privileged.sh` (see the menu bar README).
