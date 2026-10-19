#!/usr/bin/env bash
# Submits FILE (a zip of the app, or the DMG) to Apple's notary service and waits for the
# verdict; on anything but Accepted, prints the notary log and fails. release.yml runs it.
#
#   NOTARY=api-key  KEY_ID ISSUER_ID, and the key in $RUNNER_TEMP/notary/AuthKey_<KEY_ID>.p8
#   NOTARY=apple-id APPLE_ID APPLE_PASSWORD TEAM_ID
#
#   .github/scripts/notarize.sh FILE
set -euo pipefail

file="$1"
case "${NOTARY:-}" in
api-key) auth=(--key "$RUNNER_TEMP/notary/AuthKey_$KEY_ID.p8" --key-id "$KEY_ID" --issuer "$ISSUER_ID") ;;
apple-id) auth=(--apple-id "$APPLE_ID" --password "$APPLE_PASSWORD" --team-id "$TEAM_ID") ;;
*) echo "notarize.sh: NOTARY is '${NOTARY:-}', not api-key or apple-id" >&2; exit 2 ;;
esac

result="$(mktemp)"
trap 'rm -f -- "$result"' EXIT
# notarytool can exit 0 for a rejected submission, so the status decides.
xcrun notarytool submit "$file" "${auth[@]}" --wait --timeout 1h --output-format json > "$result" || true
status="$(plutil -extract status raw -o - "$result" 2>/dev/null || true)"
id="$(plutil -extract id raw -o - "$result" 2>/dev/null || true)"
echo "notarize.sh: $(basename "$file"): submission ${id:-none}, status ${status:-unknown}"
if [ "$status" != Accepted ]; then
    if [ -n "$id" ]; then
        xcrun notarytool log "$id" "${auth[@]}" || true
    else
        cat "$result" >&2
    fi
    exit 1
fi
