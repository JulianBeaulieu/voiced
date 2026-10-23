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

  # The app updates itself (ADR 0034), so `brew upgrade` leaves it to the app;
  # `brew upgrade --cask voiced` still updates it when asked.
  auto_updates true
  depends_on arch: :arm64
  depends_on macos: :sonoma

  app "voiced.app"

  # The services are not the cask's: voiced-privileged installs them when the app first
  # opens (ADR 0018). Homebrew runs `uninstall` on upgrade and reinstall too, with sudo, so
  # removing them there would cost an update a password and the app another to put them
  # back; only `--zap` removes them. `launchctl:` also deletes each label's plist in
  # /Library/LaunchDaemons.
  uninstall quit: "io.github.julianbeaulieu.voiced.menubar"

  zap launchctl: [
        "io.github.julianbeaulieu.voiced.stt",
        "io.github.julianbeaulieu.voiced.tts",
        "io.github.julianbeaulieu.voiced.watchdog",
      ],
      trash:     "~/Library/Application Support/voiced"

  caveats <<~EOS
    voiced.app installs the voiced services the first time it opens.

    `brew upgrade --cask voiced` leaves the services running.
    The app updates them on its next launch.

    `brew uninstall --cask voiced` leaves the services running.
    To remove them:
      before it, Uninstall in the app's Settings;
      or `brew uninstall --zap --cask voiced`;
      or afterwards, the `voiced uninstall` steps in docs/operations.md.
    Only the app and `voiced uninstall` can also remove ~/.voiced,
    the config in ~/.config/voiced and the downloaded models.
  EOS
end
