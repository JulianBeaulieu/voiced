# Signing and notarizing voiced.app

`bundle.sh` produces an ad-hoc signed bundle: it launches on the machine that
built it, but Gatekeeper rejects it when it is downloaded. Distributing it
needs a Developer ID signature and Apple's notarization. Nothing in this
repository holds an identity, a certificate or a password; everything below
comes from the maintainer's Apple developer account and, for CI, from
repository secrets.

## By hand

Needs a paid Apple Developer Program membership and Xcode or the command line
tools.

1. In the developer account, create a **Developer ID Application**
   certificate (Certificates, Identifiers & Profiles, or Xcode > Settings >
   Accounts > Manage Certificates) and install it in the login keychain.
   Check it is there:

   ```sh
   security find-identity -v -p codesigning
   # 1) ABCDEF... "Developer ID Application: Your Name (TEAMID1234)"
   ```

2. Create an app-specific password for your Apple ID at account.apple.com
   and store notarization credentials in the keychain once:

   ```sh
   xcrun notarytool store-credentials voiced-notary \
       --apple-id you@example.com --team-id TEAMID1234 --password <app-specific password>
   ```

3. Build, sign with the hardened runtime and a secure timestamp, and check:

   ```sh
   menubar/Scripts/bundle.sh
   codesign --force --options runtime --timestamp \
       --sign "Developer ID Application: Your Name (TEAMID1234)" menubar/build/voiced.app
   codesign --verify --strict --verbose=2 menubar/build/voiced.app
   ```

   The app needs no entitlements: it uses a Unix socket in the user's home,
   `SMAppService` for launch at login and `NSWorkspace` to open files.

4. Notarize and staple:

   ```sh
   ditto -c -k --keepParent menubar/build/voiced.app voiced.zip
   xcrun notarytool submit voiced.zip --keychain-profile voiced-notary --wait
   xcrun stapler staple menubar/build/voiced.app
   spctl --assess --type execute --verbose menubar/build/voiced.app
   # menubar/build/voiced.app: accepted, source=Notarized Developer ID
   ```

5. Zip the stapled app again (`ditto -c -k --keepParent ...`) and publish
   that zip.

## In CI

`.github/workflows/release.yml` does the same on a macOS runner when a `v*`
tag is pushed (CONTRIBUTING.md, "Cutting a release"). It signs and notarizes
only when every secret it needs is set; otherwise it builds the ad hoc bundle
and names its assets `voiced-<version>-macos-adhoc.zip` and `.dmg`. A
`workflow_dispatch` run from a branch is always ad hoc and never creates a
release.

The secrets, set by the maintainer:

| Secret | Contents |
|---|---|
| `MACOS_CERTIFICATE_P12` | The Developer ID Application certificate and its private key, exported from Keychain Access as `.p12`, base64 encoded (`base64 -i cert.p12 \| pbcopy`) |
| `MACOS_CERTIFICATE_PASSWORD` | The password chosen at export |
| `NOTARY_TEAM_ID` | The team ID, the ten characters in parentheses in the identity's name; the workflow picks the Developer ID Application identity of this team from the certificate |
| `NOTARY_KEY_P8`, `NOTARY_KEY_ID`, `NOTARY_ISSUER_ID` | An App Store Connect API key with the Developer role (Users and Access > Integrations > Team Keys): the downloaded `AuthKey_<id>.p8` as is or base64 encoded, its key ID, and the issuer ID. Preferred: scoped, revocable and not a person's login |
| `NOTARY_APPLE_ID`, `NOTARY_PASSWORD` | Instead of the API key: an Apple ID and an app-specific password for it |

Keep them as secrets of a `release` environment (Settings > Environments >
New environment), with deployment restricted to selected tags matching `v*`.
Only tag builds use that environment, so a run from a branch, even one
started by someone who can edit the workflow there, cannot read them. A tag
ruleset that limits who may create `v*` tags closes the remaining gap. The
workflow has no `pull_request` trigger, so forks never run it here.

What the job does with them:

1. Imports the `.p12` into a temporary keychain with a random password
   (`security import -T /usr/bin/codesign`, `set-key-partition-list`), puts
   it first in the search list and finds the team's "Developer ID
   Application" identity with `security find-identity`.
2. Signs inside out with `codesign --force --options runtime --timestamp`:
   `Contents/Helpers/uv`, then the app. No entitlements.
3. Zips the app, `xcrun notarytool submit --wait` (`.github/scripts/notarize.sh`,
   which prints the notary log when the verdict is not Accepted), then
   `xcrun stapler staple` and `spctl --assess`, which must say
   "Notarized Developer ID".
4. Zips the stapled app for the release, builds the DMG from it, signs the
   DMG, notarizes and staples that too.
5. Deletes the keychain and the API key file, also when a step failed.

Secrets reach only the steps that use them, through `env`, never on a
command line the log shows.
