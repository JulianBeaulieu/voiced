# Homebrew

`voiced.rb` is the formula for the
[JulianBeaulieu/homebrew-voiced](https://github.com/JulianBeaulieu/homebrew-voiced)
tap. It is reviewed here and copied to the tap's `Formula/` by hand; the
reasons for its shape are in
[ADR 0017](../../docs/decisions/0017-homebrew-packaging.md).

Trying a change before it reaches the tap (Homebrew installs formulae only
from a tap, so a local one stands in). The environment variables keep
`brew` from updating itself, upgrading other formulae or cleaning up
anything but voiced along the way:

```sh
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_INSTALL_CLEANUP=1
brew tap-new --no-git "$USER/voiced-local"
cp packaging/homebrew/voiced.rb "$(brew --repository "$USER/voiced-local")/Formula/"
brew install --HEAD --build-from-source "$USER/voiced-local/voiced"
brew test "$USER/voiced-local/voiced"
brew audit --strict --online "$USER/voiced-local/voiced"
brew style "$USER/voiced-local/voiced"
voiced install            # links ~/.voiced/bin/voiced to the keg, prints the root step
brew upgrade --fetch-HEAD "$USER/voiced-local/voiced"   # a plain upgrade is a no-op for HEAD
voiced uninstall --yes    # after the sudo step it prints, if the daemons are installed
brew uninstall voiced && brew untap "$USER/voiced-local"
```

`--HEAD` builds `main` from GitHub. To build a local branch instead, point
the `head` line of the copy at it (commit first; Homebrew clones it):
`head "file:///path/to/checkout", branch: "my-branch", using: :git`.

`cask/voiced.rb` is the cask for the menu bar app, the tap's
`Casks/voiced.rb`. Its `version` and `sha256` are stand-ins; the release
workflow fills them in.

At a release (CONTRIBUTING.md, "Cutting a release"), the release workflow
leaves a `homebrew` artifact on the tag's run: `Formula/voiced.rb` with `url`
and `sha256` set to the tag's source archive, and `Casks/voiced.rb` with the
version and the zip's checksum. Nothing pushes to the tap; after publishing
the draft release:

```sh
gh run download <run id> --repo JulianBeaulieu/voiced --name homebrew --dir homebrew
git clone https://github.com/JulianBeaulieu/homebrew-voiced && cd homebrew-voiced
cp ../homebrew/Formula/voiced.rb Formula/
cp ../homebrew/Casks/voiced.rb Casks/       # only for a signed, notarized release
brew style Formula/voiced.rb Casks/voiced.rb
git commit -am "voiced <version>" && git push
```

A cask that begins `# AD HOC BUILD` came from a run without the signing
secrets; Gatekeeper rejects that download, so it stays out of the tap (ADR
0017, "The end-user path"). The formula installs `src/voiced/requirements.lock`
and builds with `src/voiced/build-requirements.lock` from the tag, so there
are no dependency versions to update in it. Once the tap has a tagged
formula, copy the generated file back over `voiced.rb` here, so the two stay
the same.
