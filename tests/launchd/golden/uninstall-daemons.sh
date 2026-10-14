#!/bin/bash
# voiced: stop and remove the LaunchDaemons. Review, then run with sudo.
set -uo pipefail

launchctl bootout system/org.example.voiced.stt 2>/dev/null || true
rm -f /Library/LaunchDaemons/org.example.voiced.stt.plist
launchctl bootout system/org.example.voiced.tts 2>/dev/null || true
rm -f /Library/LaunchDaemons/org.example.voiced.tts.plist
launchctl bootout system/org.example.voiced.watchdog 2>/dev/null || true
rm -f /Library/LaunchDaemons/org.example.voiced.watchdog.plist
launchctl bootout system/org.example.voiced.metalprobe 2>/dev/null || true
rm -f /Library/LaunchDaemons/org.example.voiced.metalprobe.plist

echo 'voiced daemons removed.'
