#!/usr/bin/env bash
# Builds voiced.app from this package and the checkout around it: a release build of the menu
# bar app wrapped in an app bundle, with what the installer needs (ADR 0018, "What the app
# ships and pins"):
#
#   Contents/Helpers/uv                     uv at UV_VERSION, checked against UV_SHA256
#   Contents/Resources/voiced-*.whl         the voiced wheel, built from this checkout
#   Contents/Resources/requirements.lock    the lock that wheel carries
#   Contents/Resources/install-manifest.json  the voiced version, Python, the wheel's and the
#                                           lock's sha256, the locked wheels' download size and
#                                           the pinned models (scripts/app_manifest.py)
#   Contents/Resources/uv-LICENSE           uv's licence (MIT; uv is MIT or Apache-2.0)
#   Contents/Resources/NOTICE               the repository's third-party notice
#   Contents/Resources/WhatsNew.md          this version's CHANGELOG section
#
# Everything fetched is pinned by hash: uv and its licence here, the build backend and
# `packaging` by src/voiced/build-requirements.lock. The bundle is not signed with any
# identity; see SIGNING.md. Downloads are cached in menubar/build/downloads and checked
# against their sha256 every time.
#
# The wheel is built from the checkout as it is on disk, so a tree with uncommitted changes
# is refused: the bundle would not be what any commit says. --dirty builds it anyway.
# --debug builds the app in debug configuration, the only one with the installSource
# development override (README, "Installing a development voiced"); never ship it.
#
#   menubar/Scripts/bundle.sh [--dirty] [--debug] [output directory]     # default: menubar/build
set -euo pipefail

readonly UV_VERSION=0.13.1
readonly UV_SHA256=cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2
readonly UV_LICENSE_SHA256=860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c

dirty=0
configuration=release
while [ $# -gt 0 ]; do
    case "$1" in
    --dirty) dirty=1; shift ;;
    --debug) configuration=debug; shift ;;
    *) break ;;
    esac
done
package_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(cd "$package_dir/.." && pwd)"
out_dir="${1:-$package_dir/build}"
build_lock="$repo_dir/src/voiced/build-requirements.lock"
# The CPython patch version installs use (`uv python install --no-bin`), in one file that the
# scripts reading the lock use too.
python_version="$(tr -d '[:space:]' < "$package_dir/app-python-version")"

if (( ! dirty )) && [ -n "$(git -C "$repo_dir" status --porcelain)" ]; then
    echo "bundle.sh: $repo_dir has uncommitted changes; commit them, or pass --dirty" >&2
    exit 1
fi
app="$out_dir/voiced.app"
downloads="$package_dir/build/downloads"

# The app's version is the Python package's version, so both always ship as one. That is a
# PEP 440 version ("0.1.0rc1", "1.2.dev3"), but the bundle versions must be three numbers,
# so the release part is used, padded to X.Y.Z ("0.1.0", "1.2.0").
pep440="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$repo_dir/pyproject.toml" | head -n 1)"
release="$(printf '%s' "$pep440" | sed -E 's/^([0-9]+(\.[0-9]+)*).*/\1/')"
if [[ ! "$release" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
    echo "bundle.sh: no version found in $repo_dir/pyproject.toml" >&2
    exit 1
fi
IFS=. read -r major minor patch _ <<<"$release.0.0"
version="$major.${minor:-0}.${patch:-0}"

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

# fetch URL SHA256 NAME: the cached download named NAME, fetched first if it is missing or
# does not match. Prints its path.
fetch() {
    local url="$1" sum="$2" path="$downloads/$3"
    mkdir -p "$downloads"
    if [ ! -f "$path" ] || ! echo "$sum  $path" | shasum -a 256 -c -s; then
        curl --fail --silent --show-error --location --retry 3 -o "$work/download" "$url"
        if ! echo "$sum  $work/download" | shasum -a 256 -c -s; then
            echo "bundle.sh: $url does not match its pinned sha256 $sum" >&2
            exit 1
        fi
        mv -f "$work/download" "$path"
    fi
    printf '%s\n' "$path"
}

uv_archive="$(fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz" \
    "$UV_SHA256" "uv-$UV_VERSION-aarch64-apple-darwin.tar.gz")"
uv_license="$(fetch "https://raw.githubusercontent.com/astral-sh/uv/$UV_VERSION/LICENSE-MIT" \
    "$UV_LICENSE_SHA256" "uv-$UV_VERSION-LICENSE-MIT")"
tar -xzf "$uv_archive" -C "$work" uv-aarch64-apple-darwin/uv
uv="$work/uv-aarch64-apple-darwin/uv"
# Nothing from the user's uv configuration changes what this script builds.
export UV_NO_CONFIG=1

# The wheel, built from the checkout with the hashed build backend, and the lock it carries
# as package data.
"$uv" build --quiet --wheel --python "$python_version" --build-constraints "$build_lock" --require-hashes \
    --out-dir "$work/dist" "$repo_dir"
wheels=("$work/dist"/voiced-*.whl)
[ "${#wheels[@]}" -eq 1 ] && [ -f "${wheels[0]}" ] || { echo "bundle.sh: no voiced wheel was built" >&2; exit 1; }
wheel="${wheels[0]}"
mkdir -p "$work/wheel"
unzip -q "$wheel" -d "$work/wheel"
lock="$work/wheel/voiced/requirements.lock"
[ -s "$lock" ] || { echo "bundle.sh: $(basename "$wheel") carries no voiced/requirements.lock" >&2; exit 1; }

# The manifest. app_manifest.py evaluates the lock's markers and wheel tags with `packaging`,
# installed with hashes from the build lock into an environment of its own.
"$uv" venv --quiet --python "$python_version" "$work/tools"
"$uv" pip install --quiet --python "$work/tools/bin/python" --require-hashes --no-deps -r "$build_lock"
# The model pins are the ones this wheel's engines load: what its voiced.models (standard
# library only) prints. The wheel goes into the tools environment without its dependencies,
# so `-I` keeps the checkout, the working directory and the user's site out of the import.
"$uv" pip install --quiet --python "$work/tools/bin/python" --no-deps "$wheel"
"$work/tools/bin/python" -I -m voiced.models > "$work/model-pins.json"
"$work/tools/bin/python" -E -s "$repo_dir/scripts/app_manifest.py" --wheel "$wheel" --lock "$lock" \
    --models "$work/model-pins.json" --uv-lock "$repo_dir/uv.lock" > "$work/install-manifest.json"

# What's new: the CHANGELOG section for this version, else the Unreleased one.
section() {
    awk -v heading="$1" '
        /^## / { if (found) exit; title = $0; sub(/^## \[?/, "", title); sub(/\]?( .*)?$/, "", title)
                 if (title == heading) { found = 1; next } }
        found' "$repo_dir/CHANGELOG.md"
}
notes="$(section "$pep440")"
[ -n "$notes" ] || notes="$(section Unreleased)"
[ -n "$notes" ] || { echo "bundle.sh: CHANGELOG.md has no section for $pep440 or Unreleased" >&2; exit 1; }

swift build -c "$configuration" --package-path "$package_dir" --product voiced-menubar
bin_dir="$(swift build -c "$configuration" --package-path "$package_dir" --show-bin-path)"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$app/Contents/Helpers"
cp "$bin_dir/voiced-menubar" "$app/Contents/MacOS/voiced-menubar"
install -m 755 "$uv" "$app/Contents/Helpers/uv"
resources="$app/Contents/Resources"
cp "$wheel" "$lock" "$work/install-manifest.json" "$repo_dir/NOTICE" "$resources/"
cp "$uv_license" "$resources/uv-LICENSE"
printf '# What'\''s new in voiced %s\n%s\n' "$pep440" "$notes" > "$resources/WhatsNew.md"

# The icon is drawn from an SF Symbol by make-icon.swift, its source.
iconset="$work/AppIcon.iconset"
swift "$package_dir/Scripts/make-icon.swift" "$iconset"
iconutil -c icns -o "$resources/AppIcon.icns" "$iconset"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>voiced</string>
    <key>CFBundleExecutable</key>
    <string>voiced-menubar</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.julianbeaulieu.voiced.menubar</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>voiced</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$version</string>
    <key>CFBundleVersion</key>
    <string>$version</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHumanReadableCopyright</key>
    <string>MIT License</string>
</dict>
</plist>
PLIST
plutil -lint -s "$app/Contents/Info.plist"

# Apple Silicon only runs signed code. An ad-hoc signature ("-") has no identity: it seals
# the bundle so it launches, and it is replaced when the bundle is signed for real. Inside
# out: the helper first, then the app that seals it.
codesign --force --sign - --timestamp=none "$app/Contents/Helpers/uv"
codesign --force --sign - --timestamp=none "$app"
codesign --verify --strict --deep "$app"

echo "$app (version $version from $pep440, $configuration build, uv $UV_VERSION, $(basename "$wheel"), ad-hoc signed, not notarized)"
