# 0003: launchd label prefix

Date: 2026-10-14. Status: accepted, expected to change.

## Context

launchd labels are reverse-DNS names. The project has no registered domain
yet; `voiced.computer` is the intended one if the project takes off.

## Decision

The prefix is `io.github.julianbeaulieu.voiced`, giving the labels
`io.github.julianbeaulieu.voiced.stt`, `.tts` and `.watchdog`. Python code
takes it from `DEFAULT_LAUNCHD_LABEL_PREFIX` in `voiced/config.py` and
derives labels through `settings.launchd.label(service)`. The privileged
script that installs the daemons for the CLI and the menu bar app
(`voiced/launchd/voiced-privileged.sh`, ADR 0018) spells it too, since its
only input is the user name; a test keeps the two equal.

`[launchd] label_prefix` overrides the prefix for what the CLI and the
watchdog address (status, start, stop, restart, doctor), not for what is
installed: the root step installs the default labels only, and `voiced
install` prints no root step under another prefix. Amended by ADR 0018;
until then the key also changed the labels of the plists `voiced install`
staged.

## Planned change

When a domain is registered the default becomes `computer.voiced` (labels
`computer.voiced.stt` and so on). That is a one-constant change plus a
migration in the privileged script: its `install` and `uninstall` must
recognise and boot out plists with the old prefix before `install` writes
new ones. Keep the old prefix in its list for at least one release after the
switch.

## Amendment (ADR 0035)

The services are LaunchAgents that `voiced install` renders itself, so
there is no root step that installs the default labels only: a
`[launchd] label_prefix` gets agents under that prefix. The menu bar app
still knows only the default labels.
