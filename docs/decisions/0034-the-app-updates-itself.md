# 0034: The menu bar app updates itself

Date: 2026-10-23. Status: accepted. Amends 0018 ("Updating the app itself").

## Context

ADR 0018 left the app's own updates to Homebrew (`brew upgrade --cask
voiced`) or a new download of the DMG: one more signing key and feed did not
seem worth it while most users came through Homebrew. Only the Python side
updated from inside the app ("Update voiced"). The owner wants what other
menu bar apps do: the app looks for a new version in the background, and
one click installs it and relaunches, with no DMG and no Finder.

## Decision

**Sparkle 2** (MIT), the standard for this outside the App Store, as a
SwiftPM dependency pinned exactly (`2.10.0`, `menubar/Package.resolved`;
SwiftPM checks the binary framework against its checksum). No hand-rolled
updater: downloading, verifying, replacing a running bundle and relaunching
it is the part that is easy to get subtly wrong.

### The feed

One stable URL, an asset of every release:
`https://github.com/JulianBeaulieu/voiced/releases/latest/download/appcast.xml`.
GitHub redirects `latest/download` to the newest published release that is
not a draft or a pre-release, so the feed moves when a person publishes a
release, not when a tag is pushed, and a pre-release is never offered.

`scripts/release.py appcast` writes it: one item with `sparkle:version` and
`sparkle:minimumSystemVersion` (the built app's `CFBundleVersion` and
`LSMinimumSystemVersion`, read from its Info.plist; Sparkle compares the
first), `sparkle:shortVersionString` (the PEP 440 version), the release page
as the item's `link`, and an enclosure that is the release's versioned,
notarized zip (`voiced-<version>-macos.zip`), named and sized from the file
that was signed, with its EdDSA signature. The description is a link to the release page rather than HTML
made from the CHANGELOG: the app shows a "Release Notes" link and never the
description, so a Markdown converter would be code for no reader.

Installed apps read only the newest published release's asset, so every
release must carry one: a release without it leaves every installed app
without a feed (a 404 for each check) until the next release that has it.
The release workflow therefore fails a pushed tag's signed build when
`SPARKLE_PRIVATE_KEY` is missing. Every other build (a dry run, an ad hoc
build, a fork) signs its zip with a throwaway key made on the runner,
verifies it and writes the feed outside `dist`, so `sign_update` and the
generator run on every dry run and nothing is uploaded.

A pre-release maps to the same `CFBundleVersion` as its final release
(`0.5.0rc1` and `0.5.0` are both `0.5.0`), so an app on a release candidate
is not offered the final. The feed never serves pre-releases, so this
concerns only someone who installed one by hand; they install the final the
same way.

### The keys

One EdDSA pair, made on the owner's Mac with Sparkle's `generate_keys
--account voiced`. The private key lives in the login keychain (the item
"Private key for signing Sparkle updates", account `voiced`, where
`https://sparkle-project.org`) and in `SPARKLE_PRIVATE_KEY`, a secret of the
`release` environment like the notarization secrets, so only an approved
tag build can read it. The public key is in bundle.sh and goes into
Info.plist as `SUPublicEDKey`.

In the workflow `sign_update` comes from the Sparkle archive SwiftPM already
fetched and checked, reads the key on stdin (`--ed-key-file -`), and prints
the signature. `menubar/Scripts/verify-update.swift` (CryptoKit) then checks
that signature against the `SUPublicEDKey` of the app it built, so a secret
that is not the app's key fails the tag build instead of publishing an
update no installed app accepts.

### Info.plist

A release build of bundle.sh adds:

| Key | Value | Why |
|---|---|---|
| `SUFeedURL` | the feed above | our feed only |
| `SUPublicEDKey` | the public key | every archive must carry a valid signature |
| `SUVerifyUpdateBeforeExtraction` | YES | the signature is checked before anything is unpacked |
| `SUEnableAutomaticChecks` | YES | checks on by default, and Sparkle's first-launch question never appears; the switch in Settings is the user's say |
| `SUScheduledCheckInterval` | 86400 | daily |

A `--debug` bundle has none of them, so a development build never replaces
itself with a release. The app also checks once at every launch while the
checks are on (`checkForUpdatesInBackground`): Sparkle's own schedule waits
a day from the last check and skips the first launch.

A signed feed (`SURequireSignedFeed`) was left out: the app shows only the
version and a link from the feed, and the archive's signature, checked
before extraction, is what keeps a replaced asset from installing.

### Signing the bundle

bundle.sh copies `Sparkle.framework` from the build products into
`Contents/Frameworks` and adds the `@executable_path/../Frameworks` rpath to
the executable (SwiftPM links it as `@rpath/Sparkle.framework/...` with only
`@loader_path`). `menubar/Scripts/codesign-app.sh` signs every piece inside
out, without `--deep`, as Sparkle's documentation lists them: the bundled
uv, `XPCServices/Installer.xpc`, `XPCServices/Downloader.xpc` (its
entitlements kept, none in this build), `Autoupdate`, `Updater.app`, the
framework, then the app. bundle.sh runs it ad hoc and sign.sh with the
Developer ID, the hardened runtime and a secure timestamp, so the two paths
cannot sign different sets.

No sandbox and no entitlements, as before: Sparkle needs neither outside a
sandbox. Re-signing drops the `com.apple.application-identifier` Sparkle's
prebuilt `Autoupdate` carries, which a Developer ID binary without a
provisioning profile must not have. The XPC services are only needed by a
sandboxed app and could be removed; they are kept so the framework is
Sparkle's as shipped apart from the signature.

### In the app

Sparkle drives a user driver of our own (`SparkleUpdater.swift`, the only
file that imports Sparkle) instead of its standard windows. The reason is
where an update shows: in the app's own popover, beside the services, in
its own words, never as a separate alert window over whatever the user is
doing, and never as an error. The standard driver with automatic downloads
(`SUAutomaticallyUpdate`), the gentle-reminder delegate and
`updaterWillRelaunchApplication` was the alternative weighed: it downloads in
the background and installs in one click too, but its prompt is Sparkle's
window, and the popover could only point at it. It forwards
every callback to `AppUpdates`, an observable model behind the
`AppUpdater` protocol, which the views read and the tests drive with a fake:

- A found update is a card at the top of the menu bar window: "voiced X is
  available." with **Install and Relaunch**, **Later** and a link to the
  release notes, and a small neutral dot on the icon's top corner until it
  is installed or put off. One click downloads (with progress and Cancel),
  verifies, installs and relaunches: the ready-to-install question Sparkle
  asks after the download is answered yes, since the click was the decision.
- Install and Relaunch waits while voiced is installing or updating, or a
  model downloads, and the card says why: installing quits the app, and
  quitting stops the installer and the download (`applicationWillTerminate`).
- If the quit is still cancelled (a model download started meanwhile and the
  user keeps it), the card offers **Relaunch Now**, which asks Sparkle to quit
  the app again; until then the update installs when the app quits.
- **Check for Updates…** in the menu runs a check that also answers "voiced
  X is the newest version."; Settings > General has **Check for updates
  automatically**.
- An update is news, not a problem: the service rows do not change and the
  icon's dot is neither the error badge nor its tint. A failed install (a bad signature, no network) is an orange notice
  with the error until dismissed.

### After the relaunch

The new app carries a newer voiced than the installed environment, and on
an ordinary launch it would show "Update voiced". After its own update it
runs that update at once and opens the installer window with its progress,
the same flow and the same administrator prompt, if any, as the button. The
user already asked for the new version; a second question would only
delay it. The old app records the version it relaunches from
(`appUpdateRelaunchedFrom` in its defaults) as Sparkle starts the install;
the new app reads and clears it at launch. `VoicedUpdateContinuation` then
decides at the first comparison known while nothing is in the way (the
installer idle, no model download or delete, the app not translocated);
until then the decision waits. Only an install the app manages
(`source = app`) that differs is updated, once. The same version means the
install did not happen, and Homebrew's or the command line's install is left
to the usual card.

### Homebrew

The cask gets `auto_updates true`, so `brew upgrade` leaves an app that
updates itself alone and the two do not fight; `brew upgrade --cask voiced`
still updates it when asked, and an app installed by the cask updates itself
like any other. The Python side is unchanged: for `source = homebrew` the
card shows `brew upgrade voiced` and the app does not act on it.

## Measured

On this Mac (macOS 15, Apple Silicon), with two ad hoc builds of 0.4.0 and
0.4.1 under a test bundle identifier, a throwaway key and the feed on a
local HTTP server:

- The 0.4.0 copy fetched the feed within a second of launching and showed
  "voiced 0.4.1 is available."
- Install and Relaunch: the 17.1 MB zip was fetched, and the relaunched
  0.4.1 process fetched the feed about one second after the click. A
  second check then said "voiced 0.4.1 is the newest version."
- A zip signed with another key was downloaded and refused before
  extraction ("The update is improperly signed and could not be
  validated."), shown as the notice; the app kept its version.
- Sparkle adds 3.0 MB to the bundle, 1.0 MB to the zip.
- The update of voiced after the relaunch was not part of this run (the
  copies carried no installer manifest, so they never touched `~/.voiced`);
  its decision is covered by the tests with a fake installer.
- Signed with the Developer ID by codesign-app.sh, every nested piece has the
  team identifier, the hardened runtime and no entitlements, and `codesign
  --verify --strict --deep` passes. Notarization of this layout is proven by
  the first tag build after this change.

## Alternatives considered

- **Homebrew only** (ADR 0018): no updates for the DMG's users, and a cask
  user has to remember `brew upgrade`.
- **A hand-rolled updater** (fetch the zip, check a signature, swap the
  bundle, relaunch): every piece exists in Sparkle, tested on many apps,
  including the cases this would get wrong (a running bundle, an app folder
  the user cannot write, translocation, authorization).
- **Sparkle's standard user driver**, with automatic downloads, gentle
  reminders and `updaterWillRelaunchApplication` for the relaunch: Sparkle's
  own window for the prompt in a menu bar app whose voice is its popover
  (see "In the app"); the custom driver is a hundred lines of forwarding.
- **The DMG as the enclosure**: the zip is what the cask already downloads
  and is notarized and stapled; Sparkle unpacks either. A DMG enclosure
  would be needed only to rotate the EdDSA key (below).

## Consequences

- Every release must be built with `SPARKLE_PRIVATE_KEY` set; a tag build
  without it fails. The first
  appcast appears with the next tag, so the first self-update is from that
  release to the one after.
- Losing the private key means installed apps cannot verify another update.
  With `SUVerifyUpdateBeforeExtraction` the key can still be rotated by one
  release whose enclosure is the Developer ID signed DMG (Sparkle accepts a
  new key from an archive signed by the same team); SIGNING.md says how.
- SwiftPM now fetches Sparkle (a git checkout and the binary framework) for
  every build of the package, including CI's.
- The app opens a network connection of its own: the feed, at launch and
  daily, unless the switch is off. menubar/README.md says so.
- Beyond what was asked for: `verify-update.swift` and the dry-run signing
  with a throwaway key, so a wrong secret or a broken `sign_update` shows up
  before a release, and the secret in the `release` environment rather than
  the repository, as the notarization secrets are.
