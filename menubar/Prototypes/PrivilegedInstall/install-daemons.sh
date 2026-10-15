#!/bin/bash
# Throwaway prototype for ADR 0018: the one script the app runs as root, behind a single
# administrator prompt. It would ship inside the signed app bundle (Contents/Resources), so
# root never executes a file the user can edit. It copies the staged plists first and checks
# the root-owned copies, never the user-writable originals, so a plist swapped after the
# check cannot run as root.
#
#   install-daemons.sh USER STAGING_DIR LABEL...            # as root
#   VOICED_DEST=dir VOICED_NO_LAUNCHCTL=1 install-daemons.sh ...   # test without root
set -euo pipefail
user="$1"; staging="$2"; shift 2
dest="${VOICED_DEST:-/Library/LaunchDaemons}"
home="$(dscl . -read "/Users/$user" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"

fail() { echo "refused: $*" >&2; rm -f "$dest"/*.voiced-new; exit 1; }
[[ "$user" =~ ^[A-Za-z0-9._-]+$ ]] || fail "bad user name"
uid="$(id -u "$user")" || fail "no such user $user"
(( uid >= 500 )) || fail "$user is not a regular account (uid $uid)"
[ -n "$home" ] && [ -d "$home" ] || fail "no home directory for $user"

value() { /usr/bin/plutil -extract "$2" raw -o - "$1" 2>/dev/null; }

for label in "$@"; do
    [[ "$label" =~ ^[A-Za-z0-9.-]+$ ]] || fail "bad label $label"
    src="$staging/$label.plist"; dst="$dest/$label.plist"; tmp="$dst.voiced-new"
    if [ -z "${VOICED_NO_LAUNCHCTL:-}" ]; then
        /usr/bin/install -m 644 -o root -g wheel "$src" "$tmp"
    else
        /usr/bin/install -m 644 "$src" "$tmp"
    fi
    # Checks run on the copy only.
    /usr/bin/plutil -lint -s "$tmp" || { rm -f "$tmp"; fail "$label: not a plist"; }
    [ "$(value "$tmp" Label)" = "$label" ] || { rm -f "$tmp"; fail "$label: Label mismatch"; }
    [ "$(value "$tmp" UserName)" = "$user" ] || { rm -f "$tmp"; fail "$label: UserName is not $user"; }
    for key in GroupName InitGroups RootDirectory; do
        value "$tmp" "$key" >/dev/null && { rm -f "$tmp"; fail "$label: unexpected key $key"; }
    done
    program="$(value "$tmp" ProgramArguments.0)"
    [[ "$program" == "$home/"* ]] || { rm -f "$tmp"; fail "$label: program outside $home"; }
done

# Every copy passed; only now touch launchd, so a refusal never leaves a partial install.
for label in "$@"; do
    dst="$dest/$label.plist"
    if [ -z "${VOICED_NO_LAUNCHCTL:-}" ]; then
        /bin/launchctl bootout "system/$label" 2>/dev/null || true
        for _ in $(seq 1 30); do /bin/launchctl print "system/$label" >/dev/null 2>&1 || break; sleep 1; done
    fi
    mv -f "$dst.voiced-new" "$dst"
    [ -n "${VOICED_NO_LAUNCHCTL:-}" ] || /bin/launchctl bootstrap system "$dst"
    echo "installed $label"
done
