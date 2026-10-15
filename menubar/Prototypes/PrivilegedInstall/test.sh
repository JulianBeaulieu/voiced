#!/bin/bash
# Runs voiced-privileged.sh as the current user (test mode: a scratch destination, no
# launchctl) against the plists `voiced install` staged, and against crafted variants of
# them. Every crafted case must be refused with nothing written.
#   test.sh STAGING_DIR      # e.g. ~/.config/voiced/launchd after `voiced install --skip-models`
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/voiced-privileged.sh"
staging="$1"
user="$(id -un)"; home="$HOME"; venv="$home/.voiced"
prefix="io.github.julianbeaulieu.voiced"
scratch="$(mktemp -d)"; dest="$scratch/dest"; mkdir "$dest"
failures=0

b64() { base64 -i "$1" | tr -d '\n'; }
run() { VOICED_TEST_DEST="$dest" "$script" "$@"; }
install_with() {  # $1 = path of the watchdog plist to use
    run install "$user" "$venv" "$(b64 "$staging/$prefix.stt.plist")" \
        "$(b64 "$staging/$prefix.tts.plist")" "$(b64 "$1")"
}
expect_refused() {
    local name="$1" out
    shift
    find "$dest" -mindepth 1 -delete
    if out="$("$@" 2>&1)"; then
        echo "FAIL  $name: accepted"; failures=$((failures + 1))
    elif [ -n "$(ls -A "$dest")" ]; then
        echo "FAIL  $name: refused but wrote $(ls "$dest")"; failures=$((failures + 1))
    else
        echo "ok    $name: ${out##*refused: }"
    fi
}
variant() {  # $1 = name; rest = plutil edit arguments; prints the variant's path
    local f="$scratch/$1.plist"
    cp "$staging/$prefix.watchdog.plist" "$f"
    shift
    plutil "$@" "$f"
    echo "$f"
}

# The real staged plists install, and the result has only allowlisted keys.
find "$dest" -mindepth 1 -delete
if install_with "$staging/$prefix.watchdog.plist" >/dev/null; then
    keys="$(plutil -convert xml1 -o - "$dest/$prefix.watchdog.plist" | sed -n 's/^\t<key>\(.*\)<\/key>$/\1/p' | tr '\n' ' ')"
    echo "ok    staged plists install: $keys"
else
    echo "FAIL  staged plists refused"; failures=$((failures + 1))
fi

expect_refused "program ../../bin/sh" install_with "$(variant up -replace ProgramArguments.0 -string "$home/../../bin/sh")"
expect_refused "Program key" install_with "$(variant prog -insert Program -string /bin/sh)"
expect_refused "Sockets" install_with "$(variant sock -insert Sockets -json '{"L":{"SockServiceName":"22"}}')"
expect_refused "MachServices" install_with "$(variant mach -insert MachServices -json '{"com.example":true}')"
expect_refused "stdout to sudoers.d" install_with "$(variant out -replace StandardOutPath -string /etc/sudoers.d/voiced)"
expect_refused "Nice -20" install_with "$(variant nice -insert Nice -integer -20)"
expect_refused "SessionCreate" install_with "$(variant sess -insert SessionCreate -bool true)"
expect_refused "UserName root" install_with "$(variant root -replace UserName -string root)"
expect_refused "UserName missing" install_with "$(variant nouser -remove UserName)"
expect_refused "KeepAlive dict" install_with "$(variant ka -replace KeepAlive -json '{"SuccessfulExit":false}')"
expect_refused "extra argument" install_with "$(variant arg -insert ProgramArguments -string --evil -append)"
expect_refused "Label elsewhere" install_with "$(variant lbl -replace Label -string com.docker.vmnetd)"
expect_refused "key with newline" install_with "$(variant nl -insert "$(printf 'Nice\n\t<key>Label')" -string x)"
expect_refused "venv with .." run install "$user" "$home/x/../.voiced" "$(b64 "$staging/$prefix.stt.plist")" \
    "$(b64 "$staging/$prefix.tts.plist")" "$(b64 "$staging/$prefix.watchdog.plist")"
expect_refused "user root" run install root /var/root/.voiced x y z
expect_refused "not base64" run install "$user" "$venv" '!!!' x y

# Another account's install is never replaced or removed.
other() { find "$dest" -mindepth 1 -delete; cp "$staging/$prefix.stt.plist" "$dest/"; \
    plutil -replace UserName -string someoneelse "$dest/$prefix.stt.plist"; }
other
if install_with "$staging/$prefix.watchdog.plist" >/dev/null 2>&1 \
    || [ "$(plutil -extract UserName raw -o - "$dest/$prefix.stt.plist")" != someoneelse ]; then
    echo "FAIL  another user's install was replaced"; failures=$((failures + 1))
else
    echo "ok    another user's install is kept (install)"
fi
other
if run uninstall "$user" >/dev/null 2>&1 || [ ! -f "$dest/$prefix.stt.plist" ]; then
    echo "FAIL  another user's install was removed"; failures=$((failures + 1))
else
    echo "ok    another user's install is kept (uninstall)"
fi

# Uninstall of our own install removes the three plists.
find "$dest" -mindepth 1 -delete
install_with "$staging/$prefix.watchdog.plist" >/dev/null && run uninstall "$user" >/dev/null
if [ -z "$(ls -A "$dest")" ]; then echo "ok    uninstall removes our plists"; else
    echo "FAIL  uninstall left $(ls "$dest")"; failures=$((failures + 1)); fi

leftover="$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'voiced-priv.*' 2>/dev/null)"
if [ -z "$leftover" ]; then echo "ok    no temporary files left"; else
    echo "FAIL  left $leftover"; failures=$((failures + 1)); fi

find "$scratch" -delete
echo "$failures failure(s)"
exit $(( failures > 0 ))
