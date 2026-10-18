#!/usr/bin/env bash
# Builds SMDaemonProbe.app into $1 (default: a temp dir) and signs it with $2 ("-" = ad hoc,
# or a codesigning identity). The daemon plist names the current user as UserName and
# writes to $3 (default: $1/probe.out), then execs /usr/bin/id outside the bundle.
#   build.sh OUT_DIR IDENTITY [RESULT_FILE]
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$(mktemp -d)}"; identity="${2:--}"; result="${3:-$out/probe.out}"
app="$out/SMDaemonProbe.app"
rm -rf "$app"; mkdir -p "$app/Contents/MacOS" "$app/Contents/Library/LaunchDaemons"
swiftc -O -o "$app/Contents/MacOS/SMDaemonProbe" "$here/probe.swift"
clang -O2 -o "$app/Contents/MacOS/voiced-launcher" "$here/launcher.c"
cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>SMDaemonProbe</string>
<key>CFBundleIdentifier</key><string>io.github.julianbeaulieu.voiced.smprobe</string>
<key>CFBundleName</key><string>SMDaemonProbe</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.0.1</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
cat > "$app/Contents/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.smprobe.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>io.github.julianbeaulieu.voiced.smprobe</string>
<key>BundleProgram</key><string>Contents/MacOS/voiced-launcher</string>
<key>ProgramArguments</key><array>
  <string>voiced-launcher</string><string>$result</string><string>/usr/bin/id</string>
</array>
<key>UserName</key><string>$(id -un)</string>
<key>AssociatedBundleIdentifiers</key><array><string>io.github.julianbeaulieu.voiced.smprobe</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><false/>
<key>StandardOutPath</key><string>$result.stdout</string>
<key>StandardErrorPath</key><string>$result.stderr</string>
</dict></plist>
PLIST
plutil -lint -s "$app/Contents/Info.plist" "$app/Contents/Library/LaunchDaemons/"*.plist
if [ "$identity" = "-" ]; then
  codesign --force --sign - --timestamp=none "$app/Contents/MacOS/voiced-launcher"
  codesign --force --sign - --timestamp=none "$app"
else
  codesign --force --options runtime --sign "$identity" "$app/Contents/MacOS/voiced-launcher"
  codesign --force --options runtime --sign "$identity" "$app"
fi
codesign -dvv "$app" 2>&1 | grep -E "^(Authority|TeamIdentifier|Signature)" || true
echo "$app"
