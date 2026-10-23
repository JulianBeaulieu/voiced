#!/usr/bin/env bash
# Signs voiced.app and every piece of code in it, inside out, with the codesign options
# given (ADR 0018, "Signing and release CI"; ADR 0034): the bundled uv; Sparkle's helpers in
# the order Sparkle's documentation signs them (its two XPC services, Autoupdate,
# Updater.app), then the framework; then the app, which seals them all. No --deep: each piece
# is signed on its own, so none is signed with options meant for another. bundle.sh signs
# ad hoc with it and sign.sh with the Developer ID and the hardened runtime.
#
#   menubar/Scripts/codesign-app.sh APP CODESIGN_OPTION...
#   menubar/Scripts/codesign-app.sh menubar/build/voiced.app --sign - --timestamp=none
set -euo pipefail

if [ $# -lt 2 ] || [ ! -d "$1/Contents" ]; then
    echo "usage: $0 APP CODESIGN_OPTION..." >&2
    exit 2
fi
app="$1"
shift
sparkle="$app/Contents/Frameworks/Sparkle.framework"
helpers="$sparkle/Versions/B"
for path in "$app/Contents/Helpers/uv" "$helpers/XPCServices/Installer.xpc" "$helpers/XPCServices/Downloader.xpc" \
    "$helpers/Autoupdate" "$helpers/Updater.app" "$sparkle"; do
    [ -e "$path" ] || { echo "codesign-app.sh: $path is missing" >&2; exit 1; }
done

codesign --force "$@" "$app/Contents/Helpers/uv"
codesign --force "$@" "$helpers/XPCServices/Installer.xpc"
# The downloader's entitlements are Sparkle's (none in a non-sandboxed build); kept as built.
codesign --force "$@" --preserve-metadata=entitlements "$helpers/XPCServices/Downloader.xpc"
codesign --force "$@" "$helpers/Autoupdate"
codesign --force "$@" "$helpers/Updater.app"
codesign --force "$@" "$sparkle"
codesign --force "$@" "$app"
codesign --verify --strict --deep "$app"
