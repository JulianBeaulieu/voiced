#!/bin/bash
# Throwaway prototype for ADR 0018: the one script the app runs as root, behind a single
# administrator prompt. In the app its text is embedded in the signed main executable and
# run as
#   /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/bash --noprofile --norc \
#       -c "$SCRIPT" voiced-privileged install|uninstall ...
# so neither the app's environment (BASH_ENV, PATH, ...) nor a file the user can edit reaches
# root.
#
#   voiced-privileged install USER STT_B64 TTS_B64 WATCHDOG_B64
#   voiced-privileged uninstall USER
#
# The plists arrive as base64 arguments, so root never opens a path the user controls (no
# symlink or FIFO to follow). The security boundary is UserName plus the key allowlist: a
# staged plist with any other key is refused, and what is installed is rebuilt from the
# checked values only.
# Run as a normal user it is a test harness: VOICED_TEST_DEST replaces /Library/LaunchDaemons,
# and launchctl and the firewall are skipped. As root that knob is ignored.
set -euo pipefail
umask 077

readonly PREFIX="io.github.julianbeaulieu.voiced"
readonly SERVICES=(stt tts watchdog)
readonly APP_ID="io.github.julianbeaulieu.voiced.menubar"
readonly MAX_PLIST=65536
readonly ALLOWED_KEYS="Label ProgramArguments UserName RunAtLoad KeepAlive ThrottleInterval StandardOutPath StandardErrorPath ProcessType WorkingDirectory EnvironmentVariables AssociatedBundleIdentifiers"

if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    dest=/Library/LaunchDaemons; live=1
else
    dest="${VOICED_TEST_DEST:?not root: set VOICED_TEST_DEST to test}"; live=0
fi

tmpfiles=()
cleanup() {
    local f
    for f in "${tmpfiles[@]+"${tmpfiles[@]}"}"; do /bin/rm -f -- "${f:?}"; done
}
trap cleanup EXIT
fail() { echo "refused: $*" >&2; exit 1; }
value() { /usr/bin/plutil -extract "$2" raw -o - -- "$1" 2>/dev/null; }
ptype() { /usr/bin/plutil -type "$2" -- "$1" 2>/dev/null; }
has() { ptype "$1" "$2" >/dev/null; }
# Temporary files live in this root-owned 0700 directory (removed by the trap).
work="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/voiced-priv.XXXXXX")"
tmpfiles+=("$work"/raw.stt "$work"/raw.tts "$work"/raw.watchdog \
    "$work"/out.stt "$work"/out.tts "$work"/out.watchdog)
trap 'cleanup; /bin/rmdir -- "${work:?}" 2>/dev/null || true' EXIT

mode="${1:-}"; user="${2:-}"
[[ "$user" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]] || fail "bad user name"
uid="$(/usr/bin/id -u -- "$user" 2>/dev/null)" || fail "no such user $user"
(( uid >= 500 )) || fail "$user is not a regular account (uid $uid)"
home="$(/usr/bin/dscl -plist . -read "/Users/$user" NFSHomeDirectory \
    | /usr/bin/plutil -extract "dsAttrTypeStandard:NFSHomeDirectory.0" raw -o - -)" \
    || fail "no home directory for $user"
[[ "$home" == /?* && "$home" != *$'\n'* && -d "$home" ]] || fail "bad home directory for $user"

# One install per Mac: never replace or remove another user's daemons.
check_owner() {
    local existing="$dest/$1.plist" owner
    [ -e "$existing" ] || return 0
    owner="$(value "$existing" UserName || true)"
    [ "$owner" = "$user" ] || fail "voiced is already installed for ${owner:-another account} on this Mac"
}

# Check one staged plist and write the rebuilt one to $work/out.<svc>.
rebuild() {
    local svc="$1" b64="$2" label="$PREFIX.$1"
    local raw="$work/raw.$svc" out="$work/out.$svc" key line i
    (( ${#b64} <= MAX_PLIST * 4 / 3 + 4 )) || fail "$label: plist too large"
    printf '%s' "$b64" | /usr/bin/base64 -D > "$raw" 2>/dev/null || fail "$label: not base64"
    /usr/bin/plutil -lint -s -- "$raw" >/dev/null || fail "$label: not a plist"
    [ "$(ptype "$raw" Label)" = string ] || fail "$label: no Label"
    # Top-level keys, from plutil's canonical XML: one tab, then <key>. A key with a newline
    # or markup in it cannot produce a matching line, because XML escapes < and &.
    while IFS= read -r line; do
        [[ "$line" == $'\t<key>'* ]] || continue
        key="${line#$'\t<key>'}"; key="${key%</key>}"
        [[ "$line" == $'\t<key>'"$key"'</key>' && " $ALLOWED_KEYS " == *" $key "* ]] \
            || fail "$label: key not allowed: ${line#$'\t'}"
    done < <(/usr/bin/plutil -convert xml1 -o - -- "$raw")

    local voiced="$home/.voiced/bin/voiced" logs="$home/Library/Logs/voiced" throttle
    [ "$(value "$raw" Label)" = "$label" ] || fail "$label: Label mismatch"
    [ "$(value "$raw" UserName)" = "$user" ] || fail "$label: UserName is not $user"
    [ "$(ptype "$raw" RunAtLoad)" = bool ] && [ "$(value "$raw" RunAtLoad)" = true ] || fail "$label: RunAtLoad"
    [ "$(ptype "$raw" KeepAlive)" = bool ] && [ "$(value "$raw" KeepAlive)" = true ] || fail "$label: KeepAlive"
    [ "$(ptype "$raw" ThrottleInterval)" = integer ] || fail "$label: ThrottleInterval"
    throttle="$(value "$raw" ThrottleInterval)"
    (( throttle >= 1 && throttle <= 300 )) || fail "$label: ThrottleInterval out of range"
    [ "$(value "$raw" ProcessType)" = Interactive ] || fail "$label: ProcessType"
    [ "$(value "$raw" WorkingDirectory)" = "$home" ] || fail "$label: WorkingDirectory is not $home"
    [ "$(value "$raw" StandardOutPath)" = "$logs/$svc.stdout.log" ] || fail "$label: StandardOutPath"
    [ "$(value "$raw" StandardErrorPath)" = "$logs/$svc.stderr.log" ] || fail "$label: StandardErrorPath"
    local -a expected
    if [ "$svc" = watchdog ]; then expected=("$voiced" watchdog); else expected=("$voiced" serve "$svc"); fi
    [ "$(ptype "$raw" ProgramArguments)" = array ] || fail "$label: ProgramArguments"
    for i in "${!expected[@]}"; do
        [ "$(value "$raw" "ProgramArguments.$i")" = "${expected[$i]}" ] || fail "$label: ProgramArguments.$i"
    done
    if has "$raw" "ProgramArguments.${#expected[@]}"; then fail "$label: extra ProgramArguments"; fi
    if has "$raw" EnvironmentVariables; then
        [ "$(ptype "$raw" EnvironmentVariables)" = dictionary ] || fail "$label: EnvironmentVariables"
    fi
    if has "$raw" AssociatedBundleIdentifiers; then
        [ "$(value "$raw" AssociatedBundleIdentifiers.0)" = "$APP_ID" ] \
            && ! has "$raw" AssociatedBundleIdentifiers.1 || fail "$label: AssociatedBundleIdentifiers"
    fi

    # Rebuild from the checked values only.
    /usr/bin/plutil -create xml1 -- "$out"
    /usr/bin/plutil -insert Label -string "$label" -- "$out"
    /usr/bin/plutil -insert ProgramArguments -array -- "$out"
    for i in "${!expected[@]}"; do
        /usr/bin/plutil -insert ProgramArguments -string "${expected[$i]}" -append -- "$out"
    done
    /usr/bin/plutil -insert UserName -string "$user" -- "$out"
    /usr/bin/plutil -insert RunAtLoad -bool true -- "$out"
    /usr/bin/plutil -insert KeepAlive -bool true -- "$out"
    /usr/bin/plutil -insert ThrottleInterval -integer "$throttle" -- "$out"
    /usr/bin/plutil -insert StandardOutPath -string "$logs/$svc.stdout.log" -- "$out"
    /usr/bin/plutil -insert StandardErrorPath -string "$logs/$svc.stderr.log" -- "$out"
    /usr/bin/plutil -insert ProcessType -string Interactive -- "$out"
    /usr/bin/plutil -insert WorkingDirectory -string "$home" -- "$out"
    /usr/bin/plutil -insert AssociatedBundleIdentifiers -array -- "$out"
    /usr/bin/plutil -insert AssociatedBundleIdentifiers -string "$APP_ID" -append -- "$out"
    if has "$raw" EnvironmentVariables; then
        /usr/bin/plutil -insert EnvironmentVariables -json \
            "$(/usr/bin/plutil -extract EnvironmentVariables json -o - -- "$raw")" -- "$out"
    fi
    /usr/bin/plutil -lint -s -- "$out" >/dev/null || fail "$label: rebuilt plist invalid"
}

wait_gone() {
    local _
    for _ in $(/usr/bin/seq 1 30); do
        /bin/launchctl print "system/$1" >/dev/null 2>&1 || return 0
        /bin/sleep 1
    done
}

case "$mode" in
install)
    [ $# -eq 5 ] || fail "usage: install USER STT_B64 TTS_B64 WATCHDOG_B64"
    staged=("$3" "$4" "$5")
    for i in "${!SERVICES[@]}"; do
        check_owner "$PREFIX.${SERVICES[$i]}"
        rebuild "${SERVICES[$i]}" "${staged[$i]}"
    done
    # Pass 2: everything is validated; only now touch launchd. A launchctl failure here is
    # reported per label and leaves the other labels as they are; the app offers Repair.
    failed=()
    for svc in "${SERVICES[@]}"; do
        label="$PREFIX.$svc"; dst="$dest/$label.plist"; new="$dst.voiced-new"
        tmpfiles+=("$new")
        if (( live )); then
            /usr/bin/install -m 644 -o root -g wheel -- "$work/out.$svc" "$new"
            /bin/launchctl bootout "system/$label" 2>/dev/null || true
            wait_gone "$label"
        else
            /usr/bin/install -m 644 -- "$work/out.$svc" "$new"
        fi
        /bin/mv -f -- "$new" "$dst"
        if (( live )) && ! /bin/launchctl bootstrap system "$dst"; then
            failed+=("$label"); echo "failed $label" >&2; continue
        fi
        echo "installed $label"
    done
    # The services' Python is signed ad hoc and nobody can answer the firewall's prompt
    # before login, so allow it explicitly when the application firewall is on. bin/voiced is
    # a symlink to the active environment's bin/voiced; resolve its python as the user. A
    # Python outside the home (a Homebrew keg's) is shared with other programs: left alone.
    fw=/usr/libexec/ApplicationFirewall/socketfilterfw
    if (( live )) && "$fw" --getglobalstate | /usr/bin/grep -q 'enabled'; then
        entry="$(/usr/bin/sudo -n -u "$user" /bin/realpath -- "$home/.voiced/bin/voiced")" \
            || fail "no $home/.voiced/bin/voiced"
        python="$(/usr/bin/sudo -n -u "$user" /bin/realpath -- "${entry%/voiced}/python")" \
            || fail "no python next to $entry"
        if [[ "$python" == "$home/"?* ]]; then
            "$fw" --add "$python" >/dev/null && "$fw" --unblockapp "$python" >/dev/null
            echo "firewall: allowed $python"
        else
            echo "firewall: not changed, $python is outside $home"
        fi
    fi
    (( ${#failed[@]} == 0 )) || { echo "bootstrap failed: ${failed[*]}" >&2; exit 3; }
    ;;
uninstall)
    [ $# -eq 2 ] || fail "usage: uninstall USER"
    for svc in "${SERVICES[@]}"; do check_owner "$PREFIX.$svc"; done
    for svc in "${SERVICES[@]}"; do
        label="$PREFIX.$svc"
        if (( live )); then /bin/launchctl bootout "system/$label" 2>/dev/null || true; wait_gone "$label"; fi
        /bin/rm -f -- "${dest:?}/${label:?}.plist"
        echo "removed $label"
    done
    ;;
*) fail "usage: install|uninstall" ;;
esac
