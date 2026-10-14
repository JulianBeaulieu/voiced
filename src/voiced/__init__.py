"""voiced: local speech-to-text and text-to-speech server for Apple Silicon."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("voiced")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
