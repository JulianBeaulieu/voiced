#!/bin/bash
# voiced: stop and remove the LaunchDaemons. Review, then run with sudo.
set -uo pipefail

launchctl bootout system/io.github.julianbeaulieu.voiced.stt 2>/dev/null || true
rm -f /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.stt.plist
launchctl bootout system/io.github.julianbeaulieu.voiced.tts 2>/dev/null || true
rm -f /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.tts.plist
launchctl bootout system/io.github.julianbeaulieu.voiced.watchdog 2>/dev/null || true
rm -f /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.watchdog.plist

echo 'voiced daemons removed.'
