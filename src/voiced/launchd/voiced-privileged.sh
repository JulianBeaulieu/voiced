#!/bin/bash
# ADR 0018: the one script that runs as root, for the menu bar app and the CLI alike. This
# file ships in the voiced package; the app compiles the same bytes into its signed main
# executable (`.embedInCode` of a symlink to this file in menubar/Package.swift), and
# never reads a copy in its bundle. The app runs `osascript` with a fixed environment of
# PATH alone, whose `do shell script ... with administrator privileges` runs
#   /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/bash --noprofile --norc \
#       -c "$SCRIPT" voiced-privileged install|uninstall|check USER
# so neither the app's environment (SHELLOPTS, PS4, BASH_ENV, PATH, ...) nor a file the user
# can edit reaches root (PrivilegedScript.swift). `voiced install` prints
#   sudo /bin/bash <this file in the active environment> install USER
# for a terminal user, who runs a file their own account can write, as with any script
# they choose to run with sudo; sudo resets the environment first.
#
#   voiced-privileged install USER     render and bootstrap the three daemons for USER
#   voiced-privileged uninstall USER   boot out and remove them, if they are USER's
#   voiced-privileged check USER       exit 0 if the installed plists are what install would
#                                      write and every label is loaded (needs no root)
#
# The only input is the user name. Root renders the plists itself from fixed values and the
# account's home directory, so no plist or setting the user wrote is parsed as root. No
# StandardOutPath/StandardErrorPath keys: launchd would open those paths in the user's home
# as root; VOICED_STDIO_LOG tells the services to open their own logs (ADR 0006).
# Exit codes: 0 done, 1 refused (nothing written), 3 a bootstrap failed (per label on stderr),
# 4 the firewall exception could not be added, 5 the firewall is on but the Python is outside
# the home (a Homebrew keg's) and was left alone. 4 and 5 come after a successful install.
# Run as a normal user it is a test harness: VOICED_TEST_DEST replaces /Library/LaunchDaemons
# (its lock file sits beside it), launchctl is skipped, VOICED_TEST_HOME replaces the account's home and
# VOICED_TEST_FW the socketfilterfw binary (the firewall step is skipped without it). As root
# these knobs are ignored.
set -euo pipefail
umask 077

readonly PREFIX="io.github.julianbeaulieu.voiced"
readonly SERVICES=(stt tts watchdog)
readonly APP_ID="io.github.julianbeaulieu.voiced.menubar"
readonly THROTTLE=10
readonly BASE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    dest=/Library/LaunchDaemons; lockfile=/var/run/voiced-privileged.lock; live=1
    tmpbase=/private/tmp; fw=/usr/libexec/ApplicationFirewall/socketfilterfw
    test_home=""
else
    dest="${VOICED_TEST_DEST:-}"; live=0; tmpbase="${TMPDIR:-/tmp}"
    lockfile="$dest.lock"; fw="${VOICED_TEST_FW:-}"; test_home="${VOICED_TEST_HOME:-}"
fi

fail() { echo "refused: $*" >&2; exit 1; }
value() { /usr/bin/plutil -extract "$2" raw -o - -- "$1" 2>/dev/null; }
# Runs a command as the user (as root through sudo; in test mode we already are the user).
as_user() { if (( live )); then /usr/bin/sudo -n -u "$user" -- "$@"; else "$@"; fi; }

mode="${1:-}"; user="${2:-}"
[ $# -eq 2 ] || fail "usage: install|uninstall|check USER"
[ -n "$dest" ] || { [ "$mode" = check ] && dest=/Library/LaunchDaemons; } \
    || fail "not root: set VOICED_TEST_DEST to test"
[[ "$user" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]] || fail "bad user name"
uid="$(/usr/bin/id -u -- "$user" 2>/dev/null)" || fail "no such user $user"
# Regular accounts only: not root or system accounts (below 500), not nobody (4294967294).
[[ "$uid" =~ ^[0-9]+$ ]] && (( uid >= 500 && uid < 2147483648 )) \
    || fail "$user is not a regular account (uid $uid)"
# Directory Services matches names without regard to case; use the account's own spelling.
user="$(/usr/bin/id -un -- "$uid")" || fail "no name for uid $uid"

# The home is needed to render plists, not to remove them: an uninstall works without it.
home=""
if [ "$mode" != uninstall ]; then
    if [ -n "$test_home" ]; then
        home="$test_home"
    else
        home="$(/usr/bin/dscl -plist . -read "/Users/$user" NFSHomeDirectory \
            | /usr/bin/plutil -extract "dsAttrTypeStandard:NFSHomeDirectory.0" raw -o - -)" \
            || fail "no home directory for $user"
    fi
    # Directory Services may return the name decomposed (NFD); write it composed (NFC).
    home="$(printf '%s' "$home" | /usr/bin/iconv -f UTF-8-MAC -t UTF-8)" || fail "home is not UTF-8"
    [[ "$home" == /?* && "$home" != *$'\n'* && "$home" != *//* && "/$home/" != */../* \
        && -d "$home" ]] || fail "bad home directory for $user"
    [ "$(/usr/bin/stat -L -f %u -- "$home")" = "$uid" ] || fail "$home is not owned by $user"
fi
voiced="$home/.voiced/bin/voiced"

work="$(/usr/bin/mktemp -d "$tmpbase/voiced-priv.XXXXXX")"
trap '/bin/rm -rf -- "${work:?}"' EXIT

# One install or uninstall at a time: a kernel lock (flock) on fd 9, held until this shell
# exits, however it exits. Nothing to go stale, no pid to be reused; the file itself stays.
# Taken through perl, which shares the shell's open file, so the lock outlives perl: macOS 14
# has no /usr/bin/lockf. Exit 1 is "held by another run"; anything else is a lock that could
# not be taken at all, reported as such.
take_lock() {
    local rc=0
    exec 9>>"$lockfile" || fail "cannot open $lockfile"
    /usr/bin/perl -MFcntl=:flock -e \
        'open(my $fh, ">&=9") or exit 2; flock($fh, LOCK_EX | LOCK_NB) and exit 0;
         exit($!{EWOULDBLOCK} ? 1 : 2)' || rc=$?
    case "$rc" in
    0) ;;
    1) fail "another voiced install or uninstall is running" ;;
    *) fail "cannot lock $lockfile (exit $rc)" ;;
    esac
}

# One install per Mac: never replace or remove another account's daemons. Compared by uid. An
# install whose account no longer exists is orphaned: it may be replaced or removed.
check_owner() {
    local existing="$dest/$1.plist" owner owner_uid
    [ -e "$existing" ] || return 0
    owner="$(value "$existing" UserName || true)"
    if [ -n "$owner" ] && ! owner_uid="$(/usr/bin/id -u -- "$owner" 2>/dev/null)"; then
        echo "$1: orphaned install of deleted account $owner, taking it over"
        return 0
    fi
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
    # Restart while the entry point exists; a dangling bin/voiced (environment removed) stops
    # the respawn loop until it is restored (ADR 0017). The path is JSON-escaped: it has dots,
    # so it cannot be a plutil key path.
    local esc="${voiced//\\/\\\\}"; esc="${esc//\"/\\\"}"
    /usr/bin/plutil -insert KeepAlive -json "{\"PathState\":{\"$esc\":true}}" -- "$out" \
        || fail "$PREFIX.$svc: cannot render KeepAlive"
    /usr/bin/plutil -insert ThrottleInterval -integer "$THROTTLE" -- "$out"
    /usr/bin/plutil -insert ProcessType -string Interactive -- "$out"
    /usr/bin/plutil -insert WorkingDirectory -string "$home" -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables -dictionary -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.HOME -string "$home" -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.PATH -string "$home/.voiced/bin:$BASE_PATH" -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.VOICED_CONFIG -string "$home/.config/voiced/config.toml" -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.PYTHONUNBUFFERED -string 1 -- "$out"
    /usr/bin/plutil -insert EnvironmentVariables.VOICED_STDIO_LOG -string "$home/Library/Logs/voiced/$svc" -- "$out"
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
# then checked right before it is added: a regular file owned by the user inside the home
# (compared with the home's real path, so a symlinked home works). Returns 1 on failure and 2
# for a Python outside the home (a Homebrew keg's, shared with other programs: left alone).
firewall() {
    local entry python realhome
    [ -n "$fw" ] || return 0
    "$fw" --getglobalstate | /usr/bin/grep -q 'enabled' || return 0
    realhome="$(as_user /bin/realpath -- "$home")" || return 1
    entry="$(as_user /bin/realpath -- "$voiced")" || return 1
    python="$(as_user /bin/realpath -- "${entry%/voiced}/python")" || return 1
    if [[ "$python" != "$realhome/"?* ]]; then
        echo "firewall: not changed, $python is outside $realhome"; return 2
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
    rc=0; firewall || rc=$?
    case "$rc" in
    0) ;;
    2) exit 5 ;;
    *) echo "warning: the firewall exception could not be added" >&2; exit 4 ;;
    esac
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
