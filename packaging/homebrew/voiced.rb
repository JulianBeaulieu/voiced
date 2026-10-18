# Homebrew formula for voiced. Lives in this repository for review and is
# copied to the tap (github.com/JulianBeaulieu/homebrew-voiced, Formula/) after
# it lands. See docs/decisions/0017-homebrew-packaging.md for why it is built
# this way.
class Voiced < Formula
  include Language::Python::Virtualenv

  desc "Local speech-to-text and text-to-speech server for Apple Silicon"
  homepage "https://github.com/JulianBeaulieu/voiced"
  license "MIT"
  head "https://github.com/JulianBeaulieu/voiced.git", branch: "main"

  # FIRST RELEASE: there is no tagged release yet, so only `brew install --HEAD`
  # works. When v0.1.0 is tagged (with `version = "0.1.0"` in pyproject.toml),
  # uncomment these two lines and fill in the checksum
  # (`curl -sL <url> | shasum -a 256`):
  #
  #   url "https://github.com/JulianBeaulieu/voiced/archive/refs/tags/v0.1.0.tar.gz"
  #   sha256 "0000000000000000000000000000000000000000000000000000000000000000"

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
    # The daemons run this environment (through ~/.voiced/bin/voiced, see
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
      voiced runs as three LaunchDaemons that start at boot, before anyone logs
      in, as your user (never root). `brew services` cannot set that up, so:

        voiced install

      It links ~/.voiced/bin/voiced to this formula, downloads the models
      (about 2.7 GB) and prints one `sudo /bin/bash .../voiced-privileged.sh
      install <you>` line: the only step that needs root, the same script the
      menu bar app runs. Then `voiced status`.

      Upgrade with `brew upgrade --fetch-HEAD voiced` (until the first tagged
      release); once it has removed the old version, the watchdog moves the
      services to the new one. With HOMEBREW_NO_INSTALL_CLEANUP set, run
      `voiced restart all` after the upgrade instead.

      Before `brew uninstall voiced`, run `voiced uninstall` and the sudo step
      it prints; otherwise launchd stops the daemons, since their program is
      gone, but keeps them loaded.
    EOS
  end

  test do
    assert_match "voiced", shell_output("#{bin}/voiced --version")
    system libexec/"bin/python", "-c", "import mlx.core, parakeet_mlx, mlx_audio"
  end
end
