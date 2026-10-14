#!/usr/bin/env bash
# Builds voiced.app from this package: a release build of the menu bar app wrapped in an
# app bundle with an Info.plist. The bundle is not signed with any identity; see SIGNING.md.
#
#   menubar/Scripts/bundle.sh [output directory]     # default: menubar/build
set -euo pipefail

package_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(cd "$package_dir/.." && pwd)"
out_dir="${1:-$package_dir/build}"
app="$out_dir/voiced.app"

# The app's version is the Python package's version, so both always ship as one. That is a
# PEP 440 version ("0.1.0rc1", "1.2.dev3"), but the bundle versions must be three numbers,
# so the release part is used, padded to X.Y.Z ("0.1.0", "1.2.0").
pep440="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$repo_dir/pyproject.toml" | head -n 1)"
release="$(printf '%s' "$pep440" | sed -E 's/^([0-9]+(\.[0-9]+)*).*/\1/')"
if [[ ! "$release" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
    echo "bundle.sh: no version found in $repo_dir/pyproject.toml" >&2
    exit 1
fi
IFS=. read -r major minor patch _ <<<"$release.0.0"
version="$major.${minor:-0}.${patch:-0}"

swift build -c release --package-path "$package_dir" --product voiced-menubar
bin_dir="$(swift build -c release --package-path "$package_dir" --show-bin-path)"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin_dir/voiced-menubar" "$app/Contents/MacOS/voiced-menubar"

# The icon is drawn from an SF Symbol by make-icon.swift, its source.
iconset="$(mktemp -d)/AppIcon.iconset"
swift "$package_dir/Scripts/make-icon.swift" "$iconset"
iconutil -c icns -o "$app/Contents/Resources/AppIcon.icns" "$iconset"
rm -rf "$(dirname "$iconset")"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>voiced</string>
    <key>CFBundleExecutable</key>
    <string>voiced-menubar</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.julianbeaulieu.voiced.menubar</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>voiced</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$version</string>
    <key>CFBundleVersion</key>
    <string>$version</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHumanReadableCopyright</key>
    <string>MIT License</string>
</dict>
</plist>
PLIST
plutil -lint -s "$app/Contents/Info.plist"

# Apple Silicon only runs signed code. An ad-hoc signature ("-") has no identity: it seals
# the bundle so it launches, and it is replaced when the bundle is signed for real.
codesign --force --sign - --timestamp=none "$app"

echo "$app (version $version from $pep440, ad-hoc signed, not notarized)"
