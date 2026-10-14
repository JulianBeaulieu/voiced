#!/bin/bash
# voiced: one-shot LaunchDaemon that checks MLX/Metal without a GUI session.
# Installs the job, waits for its result, then removes it again (also on error).
set -euo pipefail
LABEL=org.example.voiced.metalprobe
DST=/Library/LaunchDaemons/org.example.voiced.metalprobe.plist
OUT=/Users/someone/Library/Logs/voiced/metal-probe.json
trap 'launchctl bootout system/org.example.voiced.metalprobe 2>/dev/null || true; rm -f /Library/LaunchDaemons/org.example.voiced.metalprobe.plist' EXIT
rm -f "$OUT"
launchctl bootout system/org.example.voiced.metalprobe 2>/dev/null || true
for _ in $(seq 1 30); do launchctl print system/org.example.voiced.metalprobe >/dev/null 2>&1 || break; sleep 1; done
cp /Users/someone/.config/voiced/launchd/org.example.voiced.metalprobe.plist /Library/LaunchDaemons/org.example.voiced.metalprobe.plist
chown root:wheel /Library/LaunchDaemons/org.example.voiced.metalprobe.plist
chmod 644 /Library/LaunchDaemons/org.example.voiced.metalprobe.plist
launchctl bootstrap system /Library/LaunchDaemons/org.example.voiced.metalprobe.plist
for _ in $(seq 1 60); do [ -f "$OUT" ] && break; sleep 1; done
if [ -f "$OUT" ]; then cat "$OUT"; else
  echo 'no result after 60 s; see /Users/someone/Library/Logs/voiced/metalprobe.stderr.log'; exit 1
fi
