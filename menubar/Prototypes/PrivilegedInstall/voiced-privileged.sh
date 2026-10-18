#!/bin/bash
# Throwaway prototype for ADR 0018: the one script the app runs as root, behind a single
# administrator prompt. In the app its text is compiled into the signed main executable and
# run as
#   /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/bash --noprofile --norc \
#       -c "$SCRIPT" voiced-privileged install|uninstall|check USER
# so neither the app's environment (BASH_ENV, PATH, ...) nor a file the user can edit reaches
# root.
#
#   voiced-privileged install USER     render and bootstrap the three daemons for USER
#   voiced-privileged uninstall USER   boot out and remove them, if they are USER's
#   voiced-privileged check USER       exit 0 if the installed plists are what install would
#                                      write and every label is loaded (needs no root)
#
# The only input is the user name. Root renders the plists itself from fixed values and the
# account's home directory, so nothing the user wrote is parsed as root. There are no
# StandardOutPath/StandardErrorPath keys: launchd would open those paths in the user's home
# as root, and the services open their own logs.
# Run as a normal user it is a test harness: VOICED_TEST_DEST replaces /Library/LaunchDaemons
# and holds the lock, and launchctl and the firewall are skipped. As root that knob is ignored.
set -euo pipefail
umask 077

readonly PREFIX="io.github.julianbeaulieu.voiced"
readonly SERVICES=(stt tts watchdog)
readonly APP_ID="io.github.julianbeaulieu.voiced.menubar"
readonly THROTTLE=10
readonly BASE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    dest=/Library/LaunchDaemons; lockdir=/var/run/voiced-privileged.lock; live=1
    tmpbase=/private/tmp
else
    dest="${VOICED_TEST_DEST:-}"; live=0; tmpbase="${TMPDIR:-/tmp}"
    lockdir="$dest/.lock"
fi

fail() { echo "refused: $*" >&2; exit 1; }
value() { /usr/bin/plutil -extract "$2" raw -o - -- "$1" 2>/dev/null; }

mode="${1:-}"; user="${2:-}"
[ $# -eq 2 ] || fail "usage: install|uninstall|check USER"
[ -n "$dest" ] || { [ "$mode" = check ] && dest=/Library/LaunchDaemons; } \
    || fail "not root: set VOICED_TEST_DEST to test"
[[ "$user" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]] || fail "bad user name"
uid="$(/usr/bin/id -u -- "$user" 2>/dev/null)" || fail "no such user $user"
[[ "$uid" =~ ^[0-9]+$ ]] && (( uid >= 500 )) || fail "$user is not a regular account (uid $uid)"
home="$(/usr/bin/dscl -plist . -read "/Users/$user" NFSHomeDirectory \
    | /usr/bin/plutil -extract "dsAttrTypeStandard:NFSHomeDirectory.0" raw -o - -)" \
    || fail "no home directory for $user"
# Directory Services may return the name decomposed (NFD); write it composed (NFC).
home="$(printf '%s' "$home" | /usr/bin/iconv -f UTF-8-MAC -t UTF-8)" || fail "home is not UTF-8"
[[ "$home" == /?* && "$home" != *$'\n'* && "$home" != *//* && "/$home/" != */../* \
    && -d "$home" ]] || fail "bad home directory for $user"
voiced="$home/.voiced/bin/voiced"

work="$(/usr/bin/mktemp -d "$tmpbase/voiced-priv.XXXXXX")"
locked=0
cleanup() {
    /bin/rm -rf -- "${work:?}"
    if (( locked )); then /bin/rm -f -- "$lockdir/pid"; /bin/rmdir -- "$lockdir" 2>/dev/null || true; fi
}
trap cleanup EXIT

# One install or uninstall at a time. A lock left by a process that no longer runs is taken over.
take_lock() {
    local pid
    if ! /bin/mkdir -m 700 -- "$lockdir" 2>/dev/null; then
        pid="$(/bin/cat -- "$lockdir/pid" 2>/dev/null || true)"
        if [[ "$pid" =~ ^[0-9]+$ ]] && /bin/kill -0 "$pid" 2>/dev/null; then
            fail "another voiced install or uninstall is running"
        fi
        /bin/rm -rf -- "${lockdir:?}"; /bin/mkdir -m 700 -- "$lockdir" || fail "cannot take the lock"
    fi
    locked=1; echo "$$" > "$lockdir/pid"
}

# One install per Mac: never replace or remove another account's daemons. Compared by uid.
check_owner() {
    local existing="$dest/$1.plist" owner owner_uid
    [ -e "$existing" ] || return 0
    owner="$(value "$existing" UserName || true)"
    owner_uid="$(/usr/bin/id -u -- "$owner" 2>/dev/null || true)"
    [ -n "$owner" ] && [ "$owner_uid" = "$uid" ] \
        || fail "voiced is already installed for ${owner:-another account} on this Mac"
}

# Write the plist for one service to $work/<svc>.plist, from fixed values only.
render() {
    local svc="$1" out="$work/$1.plist"
    /usr/bin/plutil -create xml1 -- "$out"
    /usr/bin/plutil -insert Label -string "$PREFIX.$svc" -- "$out"
    /usr/bin/plutil -insert ProgramArguments -array -- "$out"
    /usr/bin/plutil -insert ProgramArguments -string "$voiced" -append -- "$out"
    if [ "$svc" = watchdog ]; then
        /usr/bin/plutil -insert ProgramArguments -string watchdog -append -- "$out"
    else
        /usr/bin/plutil -insert ProgramArguments -string serve -append -- "$out"
        /usr/bin/plutil -insert ProgramArguments -string "$svc" -append -- "$out"
    fi
    /usr/bin/plutil -insert UserName -string "$user" -- "$out"
    /usr/bin/plutil -insert RunAtLoad -bool true -- "$out"
    /usr/bin/plutil -insert KeepAlive -bool true -- "$out"
    /usr/bin/plutil -insert ThrottleInterval -integer "$THROTTLE" -- "$out"
    /usr/bin/plutil -insert ProcessType -string Interactive -- "$out"
    /usr/bin/plutil -insert WorkingDirectory -string "$home" -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables -dictionary -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.HOME -string "$home" -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.PATH -string "$home/.voiced/bin:$BASE_PATH" -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.VOICED_CONFIG -string "$home/.config/voiced/config.toml" -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.PYTHONUNBUFFERED -string 1 -- "$out"
    /usr/bin/plutil -insert AssociatedBundleIdentifiers -array -- "$out"
    /usr/bin/plutil -insert AssociatedBundleIdentifiers -string "$APP_ID" -append -- "$out"
    /usr/bin/plutil -lint -s -- "$out" >/dev/null || fail "$PREFIX.$svc: rendered plist invalid"
}

wait_gone() {
    local _
    for _ in $(/usr/bin/seq 1 30); do
        /bin/launchctl print "system/$1" >/dev/null 2>&1 || return 0
        /bin/sleep 1
    done
}

# The services' Python is signed ad hoc and nobody can answer the firewall's prompt before
# login, so allow it explicitly when the application firewall is on. Resolved as the user,
# right before it is added; it must be a regular file owned by the user inside the home.
# A Python outside the home (a Homebrew keg's) is shared with other programs: left alone.
firewall() {
    local fw=/usr/libexec/ApplicationFirewall/socketfilterfw entry python
    "$fw" --getglobalstate | /usr/bin/grep -q 'enabled' || return 0
    entry="$(/usr/bin/sudo -n -u "$user" /bin/realpath -- "$voiced")" || return 1
    python="$(/usr/bin/sudo -n -u "$user" /bin/realpath -- "${entry%/voiced}/python")" || return 1
    if [[ "$python" != "$home/"?* ]]; then
        echo "firewall: not changed, $python is outside $home"; return 0
    fi
    [ -f "$python" ] && [ ! -L "$python" ] && [ "$(/usr/bin/stat -f %u -- "$python")" = "$uid" ] || return 1
    "$fw" --add "$python" >/dev/null && "$fw" --unblockapp "$python" >/dev/null || return 1
    echo "firewall: $python may accept incoming connections"
}

case "$mode" in
install)
    take_lock
    for svc in "${SERVICES[@]}"; do check_owner "$PREFIX.$svc"; render "$svc"; done
    # Everything is rendered; only now touch launchd. A launchctl failure here is reported per
    # label and leaves the other labels as they are; the app offers Repair.
    failed=()
    for svc in "${SERVICES[@]}"; do
        label="$PREFIX.$svc"; dst="$dest/$label.plist"; new="$dst.voiced-new"
        if (( live )); then
            /usr/bin/install -m 644 -o root -g wheel -- "$work/$svc.plist" "$new"
            /bin/launchctl bootout "system/$label" 2>/dev/null || true
            wait_gone "$label"
        else
            /usr/bin/install -m 644 -- "$work/$svc.plist" "$new"
        fi
        /bin/mv -f -- "$new" "$dst"
        if (( live )) && ! /bin/launchctl bootstrap system "$dst"; then
            failed+=("$label"); echo "failed $label" >&2; continue
        fi
        echo "installed $label"
    done
    (( ${#failed[@]} == 0 )) || { echo "bootstrap failed: ${failed[*]}" >&2; exit 3; }
    if (( live )) && ! firewall; then
        echo "warning: the firewall exception could not be added" >&2; exit 4
    fi
    ;;
uninstall)
    take_lock
    for svc in "${SERVICES[@]}"; do check_owner "$PREFIX.$svc"; done
    for svc in "${SERVICES[@]}"; do
        label="$PREFIX.$svc"
        if (( live )); then /bin/launchctl bootout "system/$label" 2>/dev/null || true; wait_gone "$label"; fi
        /bin/rm -f -- "${dest:?}/${label:?}.plist"
        echo "removed $label"
    done
    ;;
check)
    for svc in "${SERVICES[@]}"; do
        label="$PREFIX.$svc"; render "$svc"
        /usr/bin/cmp -s -- "$work/$svc.plist" "$dest/$label.plist" || { echo "differs $label"; exit 1; }
        if (( live )) || [ -z "${VOICED_TEST_DEST:-}" ]; then
            /bin/launchctl print "system/$label" >/dev/null 2>&1 || { echo "not loaded $label"; exit 1; }
        fi
    done
    echo "up to date"
    ;;
*) fail "usage: install|uninstall|check USER" ;;
esac
