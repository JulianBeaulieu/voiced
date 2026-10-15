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
  # works. When v0.1.0 is tagged, uncomment these two lines and fill in the
  # checksum (`curl -sL <url> | shasum -a 256`):
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
    # The daemons run this environment (through ~/.voiced, see caveats), so
    # it is isolated from the brewed Python's own site-packages.
    venv = virtualenv_create(libexec, python3, system_site_packages: false)

    # Bytecode is compiled here so the services never write into the keg.
    # Every dependency at the version and hash recorded in uv.lock, as wheels
    # from PyPI. MLX and friends ship binary wheels only for Apple Silicon;
    # building them from source resources is not practical (ADR 0017).
    ENV["UV_CACHE_DIR"] = buildpath/".uv-cache"
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    requirements = buildpath/"requirements.txt"
    system "uv", "export", "--frozen", "--no-dev", "--extra", "stt", "--extra", "tts",
           "--no-emit-project", "--no-header", "--python", venv.root/"bin/python",
           "--output-file", requirements
    system "uv", "pip", "install", "--python", venv.root/"bin/python",
           "--require-hashes", "--no-deps", "--compile-bytecode", "-r", requirements
    system "uv", "pip", "install", "--python", venv.root/"bin/python",
           "--no-deps", "--compile-bytecode", buildpath

    bin.install_symlink libexec/"bin/voiced"
  end

  def caveats
    <<~EOS
      voiced runs as three LaunchDaemons that start at boot, before anyone logs
      in, as your user (never root). `brew services` cannot set that up, so:

        voiced install
        sudo bash ~/.config/voiced/launchd/install-daemons.sh

      The first command downloads the models (about 2.7 GB) and stages the
      plists; the second is the only step that needs root. Then `voiced status`.

      After `brew upgrade voiced`, run `voiced restart all` to load the new
      version. Before `brew uninstall voiced`, run `voiced uninstall` and the
      sudo step it prints.
    EOS
  end

  test do
    assert_match "voiced", shell_output("#{bin}/voiced --version")
    system libexec/"bin/python", "-c", "import mlx.core, parakeet_mlx, mlx_audio"
  end
end
