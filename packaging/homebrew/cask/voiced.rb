# Homebrew cask for the voiced menu bar app: it installs the Developer ID signed,
# notarized voiced.app from a GitHub release (ADR 0017, "The end-user path";
# ADR 0018). Lives in this repository for review; for each tag the release
# workflow fills in `version` and the zip's `sha256`, and the result goes to the
# tap's Casks/ (see ../README.md).
cask "voiced" do
  version "0.0.0"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

  url "https://github.com/JulianBeaulieu/voiced/releases/download/v#{version}/voiced-#{version}-macos.zip"
  name "voiced"
  desc "Menu bar app for a local speech-to-text and text-to-speech server"
  homepage "https://github.com/JulianBeaulieu/voiced"

  livecheck do
    url :url
    strategy :github_latest
  end

  # No auto_updates: the app does not update itself; `brew upgrade --cask voiced`
  # does (ADR 0018, "Updating the app itself").
  depends_on arch: :arm64
  depends_on macos: :sonoma

  app "voiced.app"

  # The services are not the cask's: voiced-privileged installs them when the app first
  # opens (ADR 0018). Homebrew runs `uninstall` on upgrade and reinstall too, with sudo, so
  # removing them there would cost an update a password and the app another to put them
  # back; only `--zap` removes them.
  uninstall quit: "io.github.julianbeaulieu.voiced.menubar"

  zap launchctl: [
        "io.github.julianbeaulieu.voiced.stt",
        "io.github.julianbeaulieu.voiced.tts",
        "io.github.julianbeaulieu.voiced.watchdog",
      ],
      delete:    [
        "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.stt.plist",
        "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.tts.plist",
        "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.watchdog.plist",
      ],
      trash:     "~/Library/Application Support/voiced"

  caveats <<~EOS
    voiced.app installs the voiced services the first time it opens.

    `brew upgrade --cask voiced` keeps the services running and asks for no
    password; the app updates them on its next launch, and asks for one only
    if the release changes the services' plists.

    `brew uninstall --cask voiced` removes the app and leaves the services
    running from ~/.voiced. To remove them as well, use Uninstall in the
    app's Settings first, or run `~/.voiced/bin/voiced uninstall` and the
    sudo step it prints afterwards, or uninstall with
    `brew uninstall --zap --cask voiced`, which also removes the app's uv
    cache. Only the first two can remove ~/.voiced, the config in
    ~/.config/voiced and the downloaded models.
  EOS
end
