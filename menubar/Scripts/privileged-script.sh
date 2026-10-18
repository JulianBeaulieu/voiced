#!/bin/bash
# Prints the privileged script compiled into the app: the text between the raw string's
# delimiters in Sources/VoicedMenuBarUI/Installer/PrivilegedScript.swift. Every test that runs
# the script (test-privileged.sh, the Python tests) takes it from here, so the Swift constant
# is the one copy.
#   privileged-script.sh > voiced-privileged.sh
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
text="$(/usr/bin/sed -n '/^    static let text = #"""$/,/^"""#$/{//!p;}' \
    "$here/../Sources/VoicedMenuBarUI/Installer/PrivilegedScript.swift")"
[ "${text%%$'\n'*}" = "#!/bin/bash" ] || { echo "privileged-script.sh: cannot find the script's text" >&2; exit 2; }
printf '%s\n' "$text"
