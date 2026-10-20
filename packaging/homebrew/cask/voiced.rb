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

  uninstall launchctl: [
              "io.github.julianbeaulieu.voiced.stt",
              "io.github.julianbeaulieu.voiced.tts",
              "io.github.julianbeaulieu.voiced.watchdog",
            ],
            quit:      "io.github.julianbeaulieu.voiced.menubar",
            delete:    [
              "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.stt.plist",
              "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.tts.plist",
              "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.watchdog.plist",
            ]

  zap trash: "~/Library/Application Support/voiced"

  caveats <<~EOS
    voiced.app installs the voiced services the first time it opens.

    `brew uninstall --cask voiced` stops and removes the services, and `--zap`
    also removes the app's uv cache. Neither removes ~/.voiced, the config in
    ~/.config/voiced or the downloaded models: use Uninstall in the app's
    Settings first to remove those.
  EOS
end
