#!/bin/bash
# Runs the privileged script compiled into the app (Sources/VoicedMenuBarUI/Installer/
# PrivilegedScript.swift) as the current user, in its test mode: a scratch destination, no
# launchctl, no firewall. Checks that what root would install matches what `voiced install`
# stages, plus the app's bundle id, and that bad input, another account's install and a
# concurrent run are refused with nothing written.
#   test-privileged.sh STAGING_DIR      # plists `voiced install` staged for the default ~/.voiced
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
staging="$1"
user="$(id -un)"
prefix="io.github.julianbeaulieu.voiced"
scratch="$(mktemp -d)"; dest="$scratch/dest"; mkdir "$dest"
# The script's text, from between the raw string's delimiters.
script="$scratch/voiced-privileged.sh"
sed -n '/^    static let text = #"""$/,/^"""#$/{//!p;}' \
    "$here/../Sources/VoicedMenuBarUI/Installer/PrivilegedScript.swift" > "$script"
chmod +x "$script"
[ "$(head -n 1 "$script")" = "#!/bin/bash" ] || { echo "cannot find the script's text" >&2; exit 2; }
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

# What root renders equals the staged plists plus the app's bundle id.
clear_dest
if run install "$user" >/dev/null; then
    for svc in stt tts watchdog; do
        want="$scratch/want.$svc.plist"
        cp "$staging/$prefix.$svc.plist" "$want"
        plutil -insert AssociatedBundleIdentifiers -json "[\"$prefix.menubar\"]" "$want"
        if [ "$(canon "$want")" = "$(canon "$dest/$prefix.$svc.plist")" ]; then
            ok "$svc: rendered plist matches voiced install's"
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
expect_refused "nobody (uid 4294967294)" run install nobody
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

# A run that holds the lock blocks a second one; a lock file nobody holds does not.
hold_lock() { exec /usr/bin/perl -MFcntl=:flock -e \
    'open(my $f, ">>", $ARGV[0]) or die; flock($f, LOCK_EX) or die; sleep 10' "$1"; }
clear_dest; hold_lock "$dest.lock" & holder=$!; sleep 1
if run install "$user" >/dev/null 2>&1; then bad "ran while locked"; else ok "a held lock refuses"; fi
kill "$holder"; wait "$holder" 2>/dev/null
if run install "$user" >/dev/null 2>&1; then ok "a lock file left behind does not block"
else bad "leftover lock file blocked"; fi

# The account's own spelling of the name is used, whatever case the caller passed.
clear_dest
upper="$(printf '%s' "$user" | tr '[:lower:]' '[:upper:]')"
if run install "$upper" >/dev/null 2>&1 \
    && [ "$(plutil -extract UserName raw -o - "$dest/$prefix.stt.plist")" = "$user" ]; then
    ok "user name case: $upper installs as $user"
else bad "user name case"; fi

# An install whose account was deleted is orphaned: install and uninstall take it over.
orphan() { clear_dest; cp "$staging/$prefix.stt.plist" "$dest/"; \
    plutil -replace UserName -string voiced-deleted-account "$dest/$prefix.stt.plist"; }
orphan
if out="$(run install "$user" 2>&1)" && [[ "$out" == *orphaned* ]] \
    && [ "$(plutil -extract UserName raw -o - "$dest/$prefix.stt.plist")" = "$user" ]; then
    ok "an orphaned install is replaced, and said so"
else bad "orphaned install (install): $out"; fi
orphan
if run uninstall "$user" >/dev/null 2>&1 && [ -z "$(ls -A "$dest")" ]; then
    ok "an orphaned install is removed"
else bad "orphaned install (uninstall)"; fi
clear_dest; run install "$user" >/dev/null
if VOICED_TEST_HOME=/nonexistent-voiced-home run uninstall "$user" >/dev/null 2>&1 \
    && [ -z "$(ls -A "$dest")" ]; then ok "uninstall does not need the home"
else bad "uninstall needed the home"; fi

# A home with spaces, quotes, a backslash and non-ASCII renders exactly, KeepAlive included.
odd="$scratch/Hömé dir \"q\" 'q' \\ \$x"; mkdir "$odd"
clear_dest
if VOICED_TEST_HOME="$odd" run install "$user" >/dev/null 2>&1 \
    && python3 -c 'import plistlib, sys; p = plistlib.load(open(sys.argv[1], "rb")); h = sys.argv[2]
assert p["WorkingDirectory"] == h and p["KeepAlive"] == {"PathState": {h + "/.voiced/bin/voiced": True}}' \
        "$dest/$prefix.watchdog.plist" "$odd"; then
    ok "odd home renders exactly"
else bad "odd home"; fi

# The home must belong to the account.
expect_refused "home owned by someone else" env VOICED_TEST_HOME=/usr VOICED_TEST_DEST="$dest" "$script" install "$user"

# Firewall, with a stand-in socketfilterfw that records what it is asked to do. The home is
# reached through a symlink; the environment's python is inside its real path.
fake="$scratch/fw"; fwlog="$scratch/fw.log"
cat > "$fake" <<FW
#!/bin/sh
case "\$1" in --getglobalstate) echo "Firewall is enabled. (State = 1)" ;; *) echo "\$*" >> "$fwlog" ;; esac
FW
chmod +x "$fake"
real="$scratch/realhome"; env_bin="$real/.voiced/envs/x/bin"
mkdir -p "$env_bin" "$real/.voiced/bin"; : > "$env_bin/voiced"; : > "$env_bin/python"
ln -s "$env_bin/voiced" "$real/.voiced/bin/voiced"; ln -s "$real" "$scratch/linkhome"
fw_run() { VOICED_TEST_HOME="$scratch/linkhome" VOICED_TEST_FW="$fake" run install "$user"; }
clear_dest; rm -f "$fwlog"
if fw_run >/dev/null 2>&1 && grep -q -- "--add $(realpath "$env_bin/python")" "$fwlog"; then
    ok "firewall: python under a symlinked home is allowed"
else bad "firewall: symlinked home"; fi
rm "$env_bin/python"; ln -s /usr/bin/true "$env_bin/python"
clear_dest; rm -f "$fwlog"; fw_run >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 5 ] && [ ! -e "$fwlog" ]; then ok "firewall: python outside the home is left alone (exit 5)"
else bad "firewall: outside home gave exit $rc"; fi

# Uninstall of our own install removes the three plists.
clear_dest
run install "$user" >/dev/null && run uninstall "$user" >/dev/null
if [ -z "$(ls -A "$dest")" ]; then ok "uninstall removes our plists"; else bad "uninstall left $(ls -A "$dest")"; fi

leftover="$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'voiced-priv.*' 2>/dev/null)"
if [ -z "$leftover" ]; then ok "no temporary files left"; else bad "left $leftover"; fi

find "$scratch" -delete
echo "$failures failure(s)"
exit $(( failures > 0 ))
