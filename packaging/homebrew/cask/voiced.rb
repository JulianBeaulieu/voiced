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

  # The services are not the cask's: `voiced install` writes them as the user's
  # LaunchAgents when the app first opens (ADR 0035). Homebrew runs `uninstall` on
  # upgrade and reinstall too, so it only quits the app; `--zap` removes the services.
  # Not `launchctl:`, which also looks for each label in the system domain with sudo,
  # and so would prompt for administrator credentials: `zap` boots the agents out of
  # the user's own domain by their plists, then trashes the plists. The uid and the
  # home are the shell's, read when it runs, not when the cask is loaded.
  uninstall quit: "io.github.julianbeaulieu.voiced.menubar"

  agents = %w[stt tts watchdog].map { |s| "Library/LaunchAgents/io.github.julianbeaulieu.voiced.#{s}.plist" }
  zap script: {
        executable:   "/bin/sh",
        args:         [
          "-c",
          "/bin/launchctl bootout \"gui/$(/usr/bin/id -u)\" #{agents.map { |a| "\"$HOME/#{a}\"" }.join(" ")}",
        ],
        must_succeed: false,
      },
      trash:  ["~/Library/Application Support/voiced", *agents.map { |a| "~/#{a}" }]

  caveats <<~EOS
    voiced.app installs the voiced services the first time it opens,
    without a password. They run as you while you are logged in.

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
