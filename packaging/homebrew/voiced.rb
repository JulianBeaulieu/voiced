# Homebrew formula for voiced. Lives in this repository for review; for each tag
# the release workflow sets `url` and `sha256`, and the result goes to the tap's
# Formula/ (see README.md). See docs/decisions/0017-homebrew-packaging.md for
# why it is built this way.
class Voiced < Formula
  include Language::Python::Virtualenv

  desc "Local speech-to-text and text-to-speech server for Apple Silicon"
  homepage "https://github.com/JulianBeaulieu/voiced"
  url "https://github.com/JulianBeaulieu/voiced/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "ff2eed09cb9305281eaf36a71a8c328667d19bf5cb6de97d0018d257fe98447a"
  license "MIT"
  head "https://github.com/JulianBeaulieu/voiced.git", branch: "main"

  depends_on "uv" => :build
  depends_on arch: :arm64
  depends_on "espeak-ng"
  depends_on "ffmpeg"
  depends_on macos: :sonoma
  depends_on "python@3.12"

  def python3
    "python3.12"
  end

  def install
    # The services run this environment (through ~/.voiced/bin/voiced, see
    # caveats), so it is isolated from the brewed Python's own site-packages.
    venv = virtualenv_create(libexec, python3, system_site_packages: false)
    python = venv.root/"bin/python"

    # The same lock as every other install path (ADR 0017): each dependency
    # at the version and hash in uv.lock, as wheels from PyPI. MLX and friends
    # ship binary wheels only for Apple Silicon; building them from source
    # resources is not practical. Then voiced itself, built with the locked
    # (hashed) build backend. Bytecode is compiled here so the services never
    # write into the keg.
    ENV["UV_CACHE_DIR"] = HOMEBREW_CACHE/"uv"
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    system "uv", "pip", "install", "--python", python, "--no-deps", "--compile-bytecode",
           "--require-hashes", "-r", buildpath/"src/voiced/requirements.lock"
    system "uv", "pip", "install", "--python", python, "--no-deps", "--compile-bytecode",
           "--build-constraints", buildpath/"src/voiced/build-requirements.lock", buildpath

    bin.install_symlink libexec/"bin/voiced"
  end

  def caveats
    <<~EOS
      voiced runs as three LaunchAgents of your user: they start when you log
      in and stop when you log out, so a Mac that serves unattended keeps you
      logged in, or logs you in automatically. `brew services` cannot set them
      up, so:

        voiced install

      It links ~/.voiced/bin/voiced to this formula, downloads the models
      (about 2.7 GB) and loads the services. No sudo. Run it logged in at the
      Mac (Screen Sharing counts); over SSH alone it stops before it changes
      anything. Then `voiced status`.

      Upgrade with `brew upgrade voiced` (for a `--HEAD` install, `brew upgrade
      --fetch-HEAD voiced`); once it has removed the old version, the watchdog
      moves the services to the new one. With HOMEBREW_NO_INSTALL_CLEANUP set,
      run `voiced restart all` after the upgrade instead. After the first
      upgrade from 0.5.0 or earlier, run `voiced install` once more: from
      0.5.1 its plists have launchd open the services' stdout and stderr
      logs, and from 0.4.0 or earlier it prints the one line that removes the
      old LaunchDaemons, without which the watchdog cannot restart a hung
      service.

      Before `brew uninstall voiced`, run `voiced uninstall --yes`; otherwise
      launchd stops the services, since their program is gone, but keeps them
      loaded.
    EOS
  end

  test do
    assert_match "voiced", shell_output("#{bin}/voiced --version")
    system libexec/"bin/python", "-c", "import mlx.core, parakeet_mlx, mlx_audio"
  end
end
