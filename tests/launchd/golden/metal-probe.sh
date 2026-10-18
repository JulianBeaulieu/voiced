#!/bin/bash
# voiced: one-shot LaunchDaemon that checks MLX/Metal without a GUI session.
# Installs the job, waits for its result, then removes it again (also on error).
# Its plist is below; root reads no other file. Review, then run with sudo.
set -euo pipefail
umask 022
OUT=/Users/someone/Library/Logs/voiced/metal-probe.json
trap '/bin/launchctl bootout system/io.github.julianbeaulieu.voiced.metalprobe 2>/dev/null || true; /bin/rm -f /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.metalprobe.plist' EXIT
/usr/bin/sudo -n -u someone -- /bin/rm -f -- "$OUT"
/bin/launchctl bootout system/io.github.julianbeaulieu.voiced.metalprobe 2>/dev/null || true
for _ in $(/usr/bin/seq 1 30); do /bin/launchctl print system/io.github.julianbeaulieu.voiced.metalprobe >/dev/null 2>&1 || break; /bin/sleep 1; done
/bin/rm -f /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.metalprobe.plist
/bin/cat > /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.metalprobe.plist <<'VOICED_PLIST_END'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>io.github.julianbeaulieu.voiced.metalprobe</string>
	<key>ProgramArguments</key>
	<array>
		<string>/Users/someone/.voiced/bin/python</string>
		<string>-m</string>
		<string>voiced.launchd.metal_probe</string>
		<string>--out</string>
		<string>/Users/someone/Library/Logs/voiced/metal-probe.json</string>
	</array>
	<key>UserName</key>
	<string>someone</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<false/>
	<key>ThrottleInterval</key>
	<integer>10</integer>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>WorkingDirectory</key>
	<string>/Users/someone</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>HOME</key>
		<string>/Users/someone</string>
		<key>PATH</key>
		<string>/Users/someone/.voiced/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
		<key>VOICED_CONFIG</key>
		<string>/Users/someone/.config/voiced/config.toml</string>
		<key>PYTHONUNBUFFERED</key>
		<string>1</string>
		<key>VOICED_STDIO_LOG</key>
		<string>/Users/someone/Library/Logs/voiced/metalprobe</string>
	</dict>
</dict>
</plist>
VOICED_PLIST_END
/usr/sbin/chown root:wheel /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.metalprobe.plist
/bin/chmod 644 /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.metalprobe.plist
/bin/launchctl bootstrap system /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.metalprobe.plist
for _ in $(/usr/bin/seq 1 60); do /usr/bin/sudo -n -u someone -- /bin/test -f "$OUT" && break; /bin/sleep 1; done
if /usr/bin/sudo -n -u someone -- /bin/test -f "$OUT"; then /usr/bin/sudo -n -u someone -- /bin/cat -- "$OUT"; else
  echo 'no result after 60 s; see /Users/someone/Library/Logs/voiced/metalprobe.stderr.log'; exit 1
fi
