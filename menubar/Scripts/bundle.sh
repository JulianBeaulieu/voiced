#!/usr/bin/env bash
# Builds voiced.app from this package and the checkout around it: a release build of the menu
# bar app wrapped in an app bundle, with what the installer needs (ADR 0018, "What the app
# ships and pins"):
#
#   Contents/Helpers/uv                     uv at UV_VERSION, checked against UV_SHA256
#   Contents/Resources/voiced-*.whl         the voiced wheel, built from this checkout
#   Contents/Resources/requirements.lock    the lock that wheel carries
#   Contents/Resources/install-manifest.json  Python, the wheel's and the lock's sha256, the
#                                           locked wheels' download size, the pinned models
#   Contents/Resources/uv-LICENSE           uv's licence (MIT; uv is MIT or Apache-2.0)
#   Contents/Resources/NOTICE               the repository's third-party notice
#   Contents/Resources/WhatsNew.md          this version's CHANGELOG section
#
# The bundle is not signed with any identity; see SIGNING.md. It needs the network the first
# time (uv, then what building the wheel and the manifest need); downloads are cached in
# menubar/build/downloads and checked against their sha256 every time.
#
#   menubar/Scripts/bundle.sh [output directory]     # default: menubar/build
set -euo pipefail

readonly UV_VERSION=0.13.1
readonly UV_SHA256=cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2
readonly UV_LICENSE_SHA256=860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c
# The CPython patch version installs use (`uv python install --no-bin`): the minor version's
# link moves silently on uv's next release. scripts/notice.py evaluates the lock for it too.
readonly PYTHON_VERSION=3.12.15
# For manifest.py, which evaluates the lock's markers and wheel tags.
readonly PACKAGING=packaging==26.3

package_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(cd "$package_dir/.." && pwd)"
out_dir="${1:-$package_dir/build}"
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

# The wheel, built from the checkout, and the lock it carries as package data.
"$uv" build --quiet --wheel --python "$PYTHON_VERSION" --out-dir "$work/dist" "$repo_dir"
wheels=("$work/dist"/voiced-*.whl)
[ "${#wheels[@]}" -eq 1 ] && [ -f "${wheels[0]}" ] || { echo "bundle.sh: no voiced wheel was built" >&2; exit 1; }
wheel="${wheels[0]}"
mkdir -p "$work/wheel"
unzip -q "$wheel" -d "$work/wheel"
lock="$work/wheel/voiced/requirements.lock"
[ -s "$lock" ] || { echo "bundle.sh: $(basename "$wheel") carries no voiced/requirements.lock" >&2; exit 1; }

# The model pins are the Python side's (`python -m voiced.models`), read from the wheel just
# built, which needs only the standard library to print them. A wheel from before that module
# existed falls back to model-pins.json, in the same shape.
if [ -f "$work/wheel/voiced/models.py" ]; then
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" -- python -I -c \
        'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("voiced.models", run_name="__main__")' \
        "$work/wheel" > "$work/models.json"
else
    echo "bundle.sh: the wheel has no voiced.models; using Scripts/model-pins.json" >&2
    cp "$package_dir/Scripts/model-pins.json" "$work/models.json"
fi
"$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$PACKAGING" -- python -I \
    "$package_dir/Scripts/manifest.py" --wheel "$wheel" --lock "$lock" --uv-lock "$repo_dir/uv.lock" \
    --models "$work/models.json" --python "$PYTHON_VERSION" > "$work/install-manifest.json"

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

swift build -c release --package-path "$package_dir" --product voiced-menubar
bin_dir="$(swift build -c release --package-path "$package_dir" --show-bin-path)"

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

echo "$app (version $version from $pep440, uv $UV_VERSION, $(basename "$wheel"), ad-hoc signed, not notarized)"
