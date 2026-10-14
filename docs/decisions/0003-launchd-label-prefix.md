# 0003: launchd label prefix

Date: 2026-10-14. Status: accepted, expected to change.

## Context

launchd labels are reverse-DNS names. The project has no registered domain
yet; `voiced.computer` is the intended one if the project takes off.

## Decision

The prefix is `io.github.julianbeaulieu.voiced`, giving the labels
`io.github.julianbeaulieu.voiced.stt`, `.tts` and `.watchdog`. It is defined
once, as `DEFAULT_LAUNCHD_LABEL_PREFIX` in `voiced/config.py`, and is also a
config key (`[launchd] label_prefix`) so an installation can override it. All
code derives labels through `settings.launchd.label(service)`; nothing else
may spell the prefix out.

## Planned change

When a domain is registered the default becomes `computer.voiced` (labels
`computer.voiced.stt` and so on). That is a one-constant change plus a
migration note in the installer: `uninstall` must recognise and boot out
plists with the old prefix before `install` writes new ones. Keep the old
prefix in the uninstall search list for at least one release after the switch.
