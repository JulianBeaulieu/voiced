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

A release job on a macOS runner does the same with secrets. Suggested
secrets, all set by the maintainer in the repository settings:

| Secret | Contents |
|---|---|
| `MACOS_CERTIFICATE_P12` | The Developer ID Application certificate and private key, exported from Keychain Access as `.p12`, base64 encoded |
| `MACOS_CERTIFICATE_PASSWORD` | The password chosen at export |
| `MACOS_SIGNING_IDENTITY` | `Developer ID Application: Your Name (TEAMID1234)` |
| `NOTARY_APPLE_ID`, `NOTARY_TEAM_ID`, `NOTARY_PASSWORD` | Apple ID, team id and app-specific password for `notarytool`; or an App Store Connect API key (`NOTARY_KEY_P8`, `NOTARY_KEY_ID`, `NOTARY_ISSUER_ID`) with `notarytool --key` |

The steps:

```yaml
- name: Import the signing certificate
  env:
    P12: ${{ secrets.MACOS_CERTIFICATE_P12 }}
    P12_PASSWORD: ${{ secrets.MACOS_CERTIFICATE_PASSWORD }}
  run: |
    keychain="$RUNNER_TEMP/signing.keychain-db"
    keychain_password="$(uuidgen)"
    echo "$P12" | base64 --decode > "$RUNNER_TEMP/cert.p12"
    security create-keychain -p "$keychain_password" "$keychain"
    security set-keychain-settings -lut 21600 "$keychain"
    security unlock-keychain -p "$keychain_password" "$keychain"
    security import "$RUNNER_TEMP/cert.p12" -k "$keychain" -P "$P12_PASSWORD" -T /usr/bin/codesign
    security set-key-partition-list -S apple-tool:,apple: -k "$keychain_password" "$keychain"
    security list-keychains -d user -s "$keychain" $(security list-keychains -d user | tr -d '"')
    rm "$RUNNER_TEMP/cert.p12"
- name: Build, sign, notarize
  env:
    IDENTITY: ${{ secrets.MACOS_SIGNING_IDENTITY }}
    APPLE_ID: ${{ secrets.NOTARY_APPLE_ID }}
    TEAM_ID: ${{ secrets.NOTARY_TEAM_ID }}
    PASSWORD: ${{ secrets.NOTARY_PASSWORD }}
  run: |
    menubar/Scripts/bundle.sh
    codesign --force --options runtime --timestamp --sign "$IDENTITY" menubar/build/voiced.app
    ditto -c -k --keepParent menubar/build/voiced.app voiced.zip
    xcrun notarytool submit voiced.zip --apple-id "$APPLE_ID" --team-id "$TEAM_ID" \
        --password "$PASSWORD" --wait
    xcrun stapler staple menubar/build/voiced.app
    ditto -c -k --keepParent menubar/build/voiced.app voiced-menubar.zip
- name: Remove the keychain
  if: always()
  run: security delete-keychain "$RUNNER_TEMP/signing.keychain-db" || true
```

Run it only on tags or `workflow_dispatch`, never on pull requests: workflows
triggered from forks do not get secrets, and a signing job should not run on
code that has not been merged.
