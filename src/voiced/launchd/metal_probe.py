"""Metal availability probe, meant to be run from a launchd context.

``python -m voiced.launchd.metal_probe --out FILE`` imports MLX, runs a small
matrix multiplication on the default device and writes a JSON record of the
outcome together with facts about the process context (uid, parent pid,
``launchctl managername``, ``HOME``). The installer and ``voiced doctor`` read
that record to answer "does Metal work from a LaunchDaemon on this machine"
without guessing. Standard library plus ``mlx`` only, so it runs in any
interpreter that has MLX installed.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MATRIX_SIZE = 1024


def _managername() -> str:
    try:
        out = subprocess.run(
            ["launchctl", "managername"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"
    return out.stdout.strip() or out.stderr.strip()


def context() -> dict[str, Any]:
    """Facts about where this process is running."""
    return {
        "python": sys.executable,
        "python_version": platform.python_version(),
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "launchd_managername": _managername(),
        "home": os.environ.get("HOME"),
        "user": os.environ.get("USER"),
        "timestamp": time.time(),
    }


def probe() -> dict[str, Any]:
    """Run the Metal check. Never raises: failures are recorded in the result."""
    result: dict[str, Any] = {"ok": False, "context": context()}
    try:
        import mlx.core as mx
    except Exception as exc:
        result["error"] = f"import mlx.core failed: {type(exc).__name__}: {exc}"
        return result
    result["mlx_version"] = getattr(mx, "__version__", "unknown")
    try:
        device = mx.default_device()
        result["default_device"] = str(device)
        metal = getattr(mx, "metal", None)
        if metal is not None and hasattr(metal, "is_available"):
            result["metal_available"] = bool(metal.is_available())
        started = time.perf_counter()
        a = mx.random.normal((MATRIX_SIZE, MATRIX_SIZE))
        b = mx.random.normal((MATRIX_SIZE, MATRIX_SIZE))
        c = (a @ b).sum()
        mx.eval(c)
        result["matmul_ms"] = round((time.perf_counter() - started) * 1000, 2)
        result["matmul_finite"] = bool(mx.isfinite(c).item())
        result["device_is_gpu"] = "gpu" in str(device).lower()
        result["ok"] = bool(result["device_is_gpu"] and result["matmul_finite"])
        if not result["ok"]:
            result["error"] = f"MLX ran on {device}, not on the GPU"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that MLX can use Metal in this context.")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON result here")
    args = parser.parse_args(argv)
    result = probe()
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_suffix(args.out.suffix + ".tmp")
        tmp.write_text(text + "\n")
        tmp.replace(args.out)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
