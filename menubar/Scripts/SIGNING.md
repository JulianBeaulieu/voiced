# Signing and notarizing voiced.app

`bundle.sh` produces an ad-hoc signed bundle: it launches on the machine that
built it, but Gatekeeper rejects it when it is downloaded. Distributing it
needs a Developer ID signature and Apple's notarization. Nothing in this
repository holds an identity, a certificate or a key; everything below comes
from the maintainer's Apple developer account and, for CI, from secrets of
the `release` environment.

`sign.sh` does the signing, both by hand and in the release workflow: it
signs every piece of code in the app inside out with the hardened runtime
and a secure timestamp (`codesign-app.sh`: the bundled uv, Sparkle's two XPC
services, `Autoupdate` and `Updater.app`, the Sparkle framework, then the
app; no entitlements: the app needs none, uv runs Python as a separate
process, and Sparkle needs none outside a sandbox), notarizes and staples
the app, checks Gatekeeper accepts
it, and then makes the zip and the DMG, signing, notarizing and stapling the
DMG too.

## Setting it up, once

Needs a paid Apple Developer Program membership, Xcode or the command line
tools, and `gh` logged in as the repository owner. Run the `gh` lines from a
checkout of this repository.

1. **Certificate.** In Xcode > Settings > Accounts > Manage Certificates,
   add a **Developer ID Application** certificate. Check it, and note the
   team ID in parentheses:

   ```sh
   security find-identity -v -p codesigning
   # 1) ABCDEF... "Developer ID Application: Your Name (TEAMID1234)"
   ```

2. **Export it with its key.** In Keychain Access > login > My Certificates,
   select the "Developer ID Application" certificate **and** the private key
   under it (expand the row; select both), File > Export Items, format
   Personal Information Exchange (.p12), with a strong password. A `.p12`
   without the key cannot sign.

3. **API key for notarization.** At App Store Connect > Users and Access >
   Integrations > Team Keys, generate a key with the Developer role. Note
   the key ID and the issuer ID above the list, and download
   `AuthKey_<key ID>.p8`: it can be downloaded **only once**.

4. **The `release` environment**, usable only from `v*` tags and only after
   you approve the run:

   ```sh
   gh api -X PUT 'repos/{owner}/{repo}/environments/release' --input - <<EOF
   {"reviewers": [{"type": "User", "id": $(gh api user --jq .id)}],
    "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   EOF
   gh api -X POST 'repos/{owner}/{repo}/environments/release/deployment-branch-policies' \
       -f name='v*' -f type=tag
   ```

5. **Who may tag.** The environment limits which ref can deploy, not who
   pushes it: anyone with write access could otherwise tag a commit with a
   changed `bundle.sh` and have it signed. A tag ruleset lets only
   repository admins create, move or delete `v*` tags:

   ```sh
   gh api -X POST 'repos/{owner}/{repo}/rulesets' --input - <<'EOF'
   {"name": "release tags", "target": "tag", "enforcement": "active",
    "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
    "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}],
    "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]}
   EOF
   ```

6. **The secrets**, as environment secrets (not repository secrets). Paths
   are where steps 2 and 3 saved the files; `gh` prompts for the values
   given no input:

   ```sh
   base64 -i ~/Desktop/voiced-developer-id.p12 | gh secret set MACOS_CERTIFICATE_P12 --env release
   gh secret set MACOS_CERTIFICATE_PASSWORD --env release     # the .p12 password
   gh secret set NOTARY_TEAM_ID --env release                 # TEAMID1234
   gh secret set NOTARY_KEY_P8 --env release < ~/Downloads/AuthKey_ABC123DEFG.p8
   gh secret set NOTARY_KEY_ID --env release                  # ABC123DEFG
   gh secret set NOTARY_ISSUER_ID --env release               # the issuer ID
   gh secret list --env release
   ```

   Then delete the `.p12` and keep the `.p8` somewhere safe, or revoke and
   replace the key when it is needed again.

7. **Dry run.** Actions > release > Run workflow on `main`. It builds ad hoc
   (a branch never signs) and creates no release; the run's `dist` artifact
   holds the files. The first signed build is the first `v*` tag
   (CONTRIBUTING.md, "Cutting a release"), or a run started on a tag ref,
   which signs but still creates no release.

## By hand

With the certificate in the login keychain (step 1) and the API key (step
3), from the repository root:

```sh
menubar/Scripts/bundle.sh
NOTARY_TEAM_ID=TEAMID1234 NOTARY_KEY_ID=ABC123DEFG NOTARY_ISSUER_ID=<issuer ID> \
NOTARY_KEY_P8="$(cat ~/Downloads/AuthKey_ABC123DEFG.p8)" \
    menubar/Scripts/sign.sh menubar/build/voiced.app menubar/build/release voiced-test-macos
spctl --assess --type execute --verbose menubar/build/voiced.app
# menubar/build/voiced.app: accepted, source=Notarized Developer ID
```

`menubar/build/release` then holds the notarized `voiced-test-macos.zip`
and `.dmg`. Given `MACOS_CERTIFICATE_P12` and `MACOS_CERTIFICATE_PASSWORD`
as well, `sign.sh` imports the certificate into a temporary keychain instead
of using the login keychain, as CI does.

## In CI

`.github/workflows/release.yml` runs `sign.sh` on a macOS runner when the
build is for a tag and the six signing secrets are set; otherwise it runs
`sign.sh --adhoc`, which only packages, and names the assets
`voiced-<version>-macos-adhoc.zip` and `.dmg`. Stapling and its validation, for
the app and the DMG, are tried up to five times, 10 to 40 seconds apart,
since Apple's ticket service sometimes drops the connection right after a
notarization. The secrets:

| Secret | Contents |
|---|---|
| `MACOS_CERTIFICATE_P12` | The Developer ID Application certificate and its private key as `.p12`, base64 encoded |
| `MACOS_CERTIFICATE_PASSWORD` | The `.p12` password |
| `NOTARY_TEAM_ID` | The team ID; `sign.sh` signs with the Developer ID Application identity of this team |
| `NOTARY_KEY_P8` | The App Store Connect API key, the `.p8` as downloaded (base64 also accepted) |
| `NOTARY_KEY_ID`, `NOTARY_ISSUER_ID` | Its key ID and issuer ID |
| `SPARKLE_PRIVATE_KEY` | The EdDSA private key the app's updates are signed with, as `generate_keys -x` exports it ("Updates of the app"); only the Appcast step reads it |

Only tag builds use the `release` environment, so a run from a branch, even
one started by someone who edited the workflow there, cannot read its
secrets; the required reviewer approves each tag build, and the tag ruleset
keeps others from creating tags. The workflow has no `pull_request` trigger,
so forks never run it here.

In the job, `sign.sh` imports the `.p12` into a keychain under
`$RUNNER_TEMP` with a random password and deletes it on exit; a later step
deletes it by path in case the job was cancelled. The API key is written to a
temporary file only `sign.sh` reads and removed with it. Secrets reach only
the two steps that need them, through `env`.

## Updates of the app

The app updates itself with Sparkle (ADR 0034). Every update archive is
signed with one EdDSA key pair, separate from the Developer ID:

- **The public key** is `SPARKLE_PUBLIC_KEY` in `bundle.sh`, which writes it
  into Info.plist as `SUPublicEDKey`. It is not secret.
- **The private key** is in the maintainer's login keychain, the item
  "Private key for signing Sparkle updates" (account `voiced`, where
  `https://sparkle-project.org`), and in the `release` environment's secret
  `SPARKLE_PRIVATE_KEY`. Back up the keychain item: with it lost, installed
  apps accept no further update except through a rotation (below).

The release workflow signs `voiced-<version>-macos.zip` with it
(`sign_update --ed-key-file -`, the key on stdin), checks the signature
against the app's `SUPublicEDKey` (`verify-update.swift`), and uploads
`appcast.xml` (`scripts/release.py appcast`) for a pushed tag's signed build.
Without the secret that build fails: installed apps read only the latest
release's `appcast.xml`. Dry runs sign with a throwaway key made on the
runner and upload no appcast.

Made once, with Sparkle's tools from the package's checkout
(`menubar/.build/artifacts/sparkle/Sparkle/bin` after `swift build`):

```sh
generate_keys --account voiced      # prints the public key for bundle.sh
# The secret, through a private temporary file removed in the same command:
bash -c 'set -euo pipefail; umask 077; d="$(mktemp -d)"; trap "rm -rf -- \"$d\"" EXIT
    generate_keys --account voiced -x "$d/key" >/dev/null
    gh secret set SPARKLE_PRIVATE_KEY --env release --repo JulianBeaulieu/voiced < "$d/key"'
```

(`generate_keys -x` refuses a path that exists, so it cannot write to a
pipe.) On another Mac, `generate_keys --account voiced -f <file>` imports an
exported key.

To sign by hand: `sign_update --account voiced voiced-<version>-macos.zip`.

**Rotating the key.** The app verifies archives before unpacking them
(`SUVerifyUpdateBeforeExtraction`), so Sparkle accepts a new public key only
from an update whose archive is a Developer ID signed disk image of the same
team. To rotate: make a new pair (`generate_keys --account voiced-<year>`),
put its public key in `bundle.sh`, set `SPARKLE_PRIVATE_KEY` to it, and for
that one release make the appcast's enclosure the release's notarized
`voiced-<version>-macos.dmg` (signed with the new key). `release.py appcast`
writes the zip, so that release's `appcast.xml` is edited by hand: the
enclosure's `url`, `length` and `sparkle:edSignature` for the DMG, then
uploaded over the draft's asset (`gh release upload v<version> appcast.xml
--clobber`) before publishing. Releases after it go back to the zip. Never
change the Developer ID and the EdDSA key in the same release: Sparkle needs
one of them to stay the same.
