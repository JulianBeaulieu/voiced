# Homebrew

`voiced.rb` is the formula for the
[JulianBeaulieu/homebrew-voiced](https://github.com/JulianBeaulieu/homebrew-voiced)
tap. It is reviewed here and copied to the tap's `Formula/` by hand; the
reasons for its shape are in
[ADR 0017](../../docs/decisions/0017-homebrew-packaging.md).

Trying a change before it reaches the tap (Homebrew installs formulae only
from a tap, so a local one stands in):

```sh
brew tap-new --no-git "$USER/voiced-local"
cp packaging/homebrew/voiced.rb "$(brew --repository "$USER/voiced-local")/Formula/"
brew install --HEAD --build-from-source "$USER/voiced-local/voiced"
brew test "$USER/voiced-local/voiced"
brew audit --strict --online "$USER/voiced-local/voiced"
voiced doctor
brew uninstall voiced && brew untap "$USER/voiced-local"
```

`--HEAD` builds `main` from GitHub. To build a local branch instead, point
the `head` line of the copy at it:
`head "file:///path/to/checkout", branch: "my-branch", using: :git`.

At a release: uncomment `url` and `sha256` in the formula with the tag's
archive and its checksum (`curl -sL <url> | shasum -a 256`), then copy the
file to the tap.
