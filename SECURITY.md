# Security policy

## Supported versions

Security fixes go into the latest minor release. Update to the newest
release before you report; the fix will land there too.

| Version | Supported |
|---|---|
| 0.4.x | yes |
| 0.3.x | no |
| 0.2.x | no |
| 0.1.x | no |
| older | no |

## Reporting a vulnerability

Report it privately through GitHub:
[open a private vulnerability report](https://github.com/JulianBeaulieu/voiced/security/advisories/new).
Do not open a public issue or discussion for a vulnerability.

Say what you found, how to reproduce it, and what an attacker gains. The
voiced version (`voiced --version`), your macOS version and how you
installed voiced (the menu bar app, the Homebrew formula or a checkout)
help.

voiced has one maintainer, who works on it part time. Expect an
acknowledgement within a week. Once the issue is confirmed, the maintainer
agrees a fix and a publication date with you, and credits you in the
advisory unless you prefer not to be.

## Scope

In scope:

- The HTTP listeners (ports 8880 and 8881) and the Wyoming listeners
  (10200 and 10300).
- The API key: requests that get past it, and ways it leaks.
- The installer: `voiced install`, `voiced uninstall`, and the privileged
  script that installs the LaunchDaemons as root.
- The launchd pieces: the plists, the services and the watchdog.
- The menu bar app, its installer, and the watchdog's control socket
  (`~/.voiced/control.sock`).

Out of scope: the models, espeak-ng, ffmpeg and the other third-party
packages themselves (report those upstream), and anything that needs an
attacker who can already run code as your user.

## Threat model

voiced is built for a Mac at home or in a small office.

- By default both services bind to `127.0.0.1` and only the Mac itself can
  reach them.
- On the local network (`bind = "0.0.0.0"`), the `/v1` routes of the HTTP
  API need the API key, once you set one. `/health`, `/`, `/metrics.json`
  and `/docs` stay open without it; with a key set they leave out error
  details. The Wyoming protocol has no authentication, so anyone on the
  network can use those ports.
- voiced is not designed to face the internet. It does no TLS and no rate
  limiting. Do not forward its ports from your router.
- Nothing runs as root except the privileged script, which you run with
  `sudo` or approve in the app's administrator prompt. The services and the
  watchdog run as your user.
- The control socket accepts only processes of your own user.

The README's [security note on `bind`](README.md#security-note-on-bind)
has the details.
