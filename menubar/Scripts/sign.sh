#!/usr/bin/env bash
# Packages voiced.app for a release (ADR 0018, "Signing and release CI"): NAME.zip and
# NAME.dmg in OUT. By default it first signs the app with the Developer ID, inside out (the
# bundled uv, Sparkle and its helpers, then the app: codesign-app.sh, which also verifies
# the result) with the hardened runtime and a secure timestamp, notarizes and
# staples it, and checks Gatekeeper accepts it; then it signs, notarizes and staples the DMG
# too. With --adhoc it only packages the ad hoc bundle bundle.sh made. The release workflow
# and SIGNING.md ("By hand") both run it.
#
#   menubar/Scripts/sign.sh [--adhoc] APP OUT NAME
#
# Environment, unless --adhoc:
#   NOTARY_TEAM_ID       the team whose "Developer ID Application" identity signs
#   NOTARY_KEY_P8        the App Store Connect API key: the .p8 as downloaded, or base64
#   NOTARY_KEY_ID, NOTARY_ISSUER_ID   its key ID and issuer ID
#   MACOS_CERTIFICATE_P12, MACOS_CERTIFICATE_PASSWORD   optional: the base64 .p12 and its
#       password, imported into a temporary keychain at SIGN_KEYCHAIN (default: in a
#       temporary directory) that is deleted on exit. Without them the identity comes from
#       the keychain search list: by hand, the login keychain.
#
# No entitlements: the app needs none, uv runs Python as a separate process, and Sparkle
# needs none outside a sandbox (ADR 0034).
set -euo pipefail

adhoc=0
if [ "${1:-}" = --adhoc ]; then adhoc=1; shift; fi
if [ $# -ne 3 ]; then
    echo "usage: $0 [--adhoc] APP OUT NAME" >&2
    exit 2
fi
app="$1" out="$2" name="$3"
[ -d "$app/Contents" ] || { echo "sign.sh: $app is not an app bundle" >&2; exit 1; }
mkdir -p "$out"

work="$(mktemp -d)"
keychain="${SIGN_KEYCHAIN:-$work/signing.keychain-db}"
created_keychain=0
cleanup() {
    if (( created_keychain )); then security delete-keychain "$keychain" 2>/dev/null || true; fi
    rm -rf -- "$work"
}
trap cleanup EXIT

# notarize FILE: submits FILE and waits for the verdict; on anything but Accepted, prints
# the notary log and fails. notarytool can exit 0 for a rejected submission, so the status
# decides.
notarize() {
    local result="$work/notary.json" status id
    xcrun notarytool submit "$1" "${notary[@]}" --wait --timeout 1h --output-format json \
        > "$result" || true
    status="$(plutil -extract status raw -o - "$result" 2>/dev/null || true)"
    id="$(plutil -extract id raw -o - "$result" 2>/dev/null || true)"
    echo "sign.sh: notarized $(basename "$1"): submission ${id:-none}, status ${status:-unknown}"
    if [ "$status" != Accepted ]; then
        if [ -n "$id" ]; then xcrun notarytool log "$id" "${notary[@]}" || true; else cat "$result" >&2; fi
        exit 1
    fi
}

# stapler ACTION FILE: runs `xcrun stapler ACTION FILE` (staple or validate), up to five
# times, 10, 20, 30 and 40 s apart. Apple's ticket delivery is eventually consistent and its
# lookup endpoint drops connections, so right after notarization stapling sometimes fails
# and works seconds later. After the last attempt, prints the stapler's output and fails.
stapler() {
    local output="$work/stapler.txt" attempt
    for attempt in 1 2 3 4 5; do
        if xcrun stapler "$1" "$2" > "$output" 2>&1; then
            cat "$output"
            return
        fi
        [ "$attempt" -lt 5 ] || break
        echo "sign.sh: stapler $1 $(basename "$2") failed ($attempt of 5); again in $((attempt * 10)) s" >&2
        sleep $((attempt * 10))
    done
    cat "$output" >&2
    exit 1
}

if (( ! adhoc )); then
    for var in NOTARY_TEAM_ID NOTARY_KEY_P8 NOTARY_KEY_ID NOTARY_ISSUER_ID; do
        [ -n "${!var:-}" ] || { echo "sign.sh: $var is not set" >&2; exit 1; }
    done

    keychain_args=()
    if [ -n "${MACOS_CERTIFICATE_P12:-}" ]; then
        # A keychain of its own with a random password, first in the search list.
        keychain_password="$(uuidgen)"
        [ -z "${GITHUB_ACTIONS:-}" ] || echo "::add-mask::$keychain_password"
        printf '%s' "$MACOS_CERTIFICATE_P12" | base64 --decode > "$work/certificate.p12"
        security create-keychain -p "$keychain_password" "$keychain"
        created_keychain=1
        security set-keychain-settings -lut 21600 "$keychain"
        security unlock-keychain -p "$keychain_password" "$keychain"
        security import "$work/certificate.p12" -k "$keychain" -f pkcs12 \
            -P "${MACOS_CERTIFICATE_PASSWORD:-}" -T /usr/bin/codesign
        rm -f "$work/certificate.p12"
        security set-key-partition-list -S apple-tool:,apple:,codesign: -s \
            -k "$keychain_password" "$keychain" > /dev/null
        searched=()
        while IFS= read -r line; do
            line="${line#"${line%%[![:space:]]*}"}"
            searched+=("${line//\"/}")
        done < <(security list-keychains -d user)
        security list-keychains -d user -s "$keychain" "${searched[@]}"
        keychain_args=(--keychain "$keychain")
    fi
    # The team's Developer ID Application identity, by its SHA-1 hash.
    identity="$(security find-identity -v -p codesigning |
        awk -v team="($NOTARY_TEAM_ID)\"" '/"Developer ID Application: / && index($0, team) { print $2; exit }')"
    if [ -z "$identity" ]; then
        echo "sign.sh: no Developer ID Application identity for team $NOTARY_TEAM_ID" >&2
        exit 1
    fi
    sign() { codesign --force --timestamp --sign "$identity" ${keychain_args[@]+"${keychain_args[@]}"} "$@"; }

    if printf '%s' "$NOTARY_KEY_P8" | grep -q -- '-----BEGIN PRIVATE KEY-----'; then
        printf '%s\n' "$NOTARY_KEY_P8" > "$work/AuthKey_$NOTARY_KEY_ID.p8"
    else
        printf '%s' "$NOTARY_KEY_P8" | base64 --decode > "$work/AuthKey_$NOTARY_KEY_ID.p8"
    fi
    notary=(--key "$work/AuthKey_$NOTARY_KEY_ID.p8" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID")

    "$(dirname "$0")/codesign-app.sh" "$app" --timestamp --options runtime --sign "$identity" \
        ${keychain_args[@]+"${keychain_args[@]}"}
    ditto -c -k --sequesterRsrc --keepParent "$app" "$work/notarize.zip"
    notarize "$work/notarize.zip"
    stapler staple "$app"
    stapler validate "$app"
    spctl --assess --type execute -vv "$app" 2>&1 | tee "$work/spctl.txt"
    grep -q "source=Notarized Developer ID" "$work/spctl.txt"
fi

ditto -c -k --sequesterRsrc --keepParent "$app" "$out/$name.zip"

# The DMG: the app and a link to /Applications. hdiutil on hosted runners sometimes fails
# with "Resource busy"; try again.
staging="$work/dmg"
mkdir -p "$staging"
ditto "$app" "$staging/voiced.app"
ln -s /Applications "$staging/Applications"
version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist")"
for attempt in 1 2 3 4 5; do
    if hdiutil create -volname "voiced $version" -srcfolder "$staging" -fs HFS+ -format UDZO \
        -ov "$out/$name.dmg"; then
        break
    fi
    [ "$attempt" -lt 5 ] || exit 1
    sleep $((attempt * 5))
done

if (( ! adhoc )); then
    sign "$out/$name.dmg"
    notarize "$out/$name.dmg"
    stapler staple "$out/$name.dmg"
    stapler validate "$out/$name.dmg"
    spctl --assess --type open --context context:primary-signature -vv "$out/$name.dmg"
fi
echo "$out/$name.zip"
echo "$out/$name.dmg"
