#!/bin/bash
# Runs voiced-privileged.sh as the current user (test mode: a scratch destination, no
# launchctl, no firewall). Checks that what root would install matches what `voiced install`
# stages, apart from the log keys root no longer writes, and that bad input, another account's
# install and a concurrent run are refused with nothing written.
#   test.sh STAGING_DIR      # plists `voiced install` staged for the default ~/.voiced
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/voiced-privileged.sh"
staging="$1"
user="$(id -un)"
prefix="io.github.julianbeaulieu.voiced"
scratch="$(mktemp -d)"; dest="$scratch/dest"; mkdir "$dest"
failures=0

run() { VOICED_TEST_DEST="$dest" "$script" "$@"; }
ok() { echo "ok    $1"; }
bad() { echo "FAIL  $1"; failures=$((failures + 1)); }
clear_dest() { find "$dest" -mindepth 1 -delete; }
# A plist as sorted JSON, so two plists compare by content, not key order.
canon() { python3 -c 'import json, plistlib, sys; print(json.dumps(plistlib.load(open(sys.argv[1], "rb")), sort_keys=True))' "$1"; }
expect_refused() {
    local name="$1" out
    shift
    clear_dest
    if out="$("$@" 2>&1)"; then bad "$name: accepted"
    elif [ -n "$(ls -A "$dest")" ]; then bad "$name: refused but wrote $(ls -A "$dest")"
    else ok "$name: ${out##*refused: }"; fi
}

# What root renders equals the staged plists without the log keys, plus the app's bundle id.
clear_dest
if run install "$user" >/dev/null; then
    for svc in stt tts watchdog; do
        want="$scratch/want.$svc.plist"
        cp "$staging/$prefix.$svc.plist" "$want"
        plutil -remove StandardOutPath "$want"; plutil -remove StandardErrorPath "$want"
        plutil -insert AssociatedBundleIdentifiers -json "[\"$prefix.menubar\"]" "$want"
        if [ "$(canon "$want")" = "$(canon "$dest/$prefix.$svc.plist")" ]; then
            ok "$svc: rendered plist matches voiced install's, without log paths"
        else
            bad "$svc: rendered plist differs"; diff <(canon "$want") <(canon "$dest/$prefix.$svc.plist")
        fi
    done
else
    bad "install refused"
fi
if run check "$user" >/dev/null; then ok "check: up to date after install"; else bad "check after install"; fi
plutil -replace ThrottleInterval -integer 1 "$dest/$prefix.tts.plist"
if run check "$user" >/dev/null; then bad "check missed a changed plist"; else ok "check: a changed plist differs"; fi

expect_refused "user root" run install root
expect_refused "uid below 500" run install daemon
expect_refused "no such user" run install no-such-user-voiced
expect_refused "bad user name" run install '../etc'
expect_refused "extra argument" run install "$user" extra
expect_refused "no mode" run "" "$user"

# Another account's install is never replaced or removed.
other() { clear_dest; cp "$staging/$prefix.stt.plist" "$dest/"; \
    plutil -replace UserName -string root "$dest/$prefix.stt.plist"; }
other
if run install "$user" >/dev/null 2>&1 \
    || [ "$(plutil -extract UserName raw -o - "$dest/$prefix.stt.plist")" != root ]; then
    bad "another user's install was replaced"
else ok "another user's install is kept (install)"; fi
other
if run uninstall "$user" >/dev/null 2>&1 || [ ! -f "$dest/$prefix.stt.plist" ]; then
    bad "another user's install was removed"
else ok "another user's install is kept (uninstall)"; fi

# A run that holds the lock blocks a second one; a lock left by a dead process does not.
clear_dest; mkdir "$dest/.lock"; echo $$ > "$dest/.lock/pid"
if run install "$user" >/dev/null 2>&1; then bad "ran while locked"; else ok "a held lock refuses"; fi
echo 999999 > "$dest/.lock/pid"
if run install "$user" >/dev/null 2>&1 && [ ! -e "$dest/.lock" ]; then ok "a stale lock is taken over"
else bad "stale lock"; fi

# Uninstall of our own install removes the three plists.
clear_dest
run install "$user" >/dev/null && run uninstall "$user" >/dev/null
if [ -z "$(ls -A "$dest")" ]; then ok "uninstall removes our plists"; else bad "uninstall left $(ls -A "$dest")"; fi

leftover="$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'voiced-priv.*' 2>/dev/null)"
if [ -z "$leftover" ]; then ok "no temporary files left"; else bad "left $leftover"; fi

find "$scratch" -delete
echo "$failures failure(s)"
exit $(( failures > 0 ))
