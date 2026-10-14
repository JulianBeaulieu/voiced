"""``voiced`` command line.

Phase 0 ships the command tree with every lifecycle command stubbed. ``serve``
and ``config`` work today so the API can be exercised with the fake engines:

    VOICED_TTS__ENGINE=fake voiced serve tts
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from voiced import __version__
from voiced.config import Settings, config_path, load_settings

LIFECYCLE = ("install", "start", "stop", "status", "logs", "uninstall", "doctor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voiced",
        description="Local speech-to-text and text-to-speech server for Apple Silicon.",
    )
    parser.add_argument("--version", action="version", version=f"voiced {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in LIFECYCLE:
        sub.add_parser(name, help=f"{name} the voiced services (not implemented yet)")

    serve = sub.add_parser("serve", help="run one service in the foreground")
    serve.add_argument("service", choices=("stt", "tts"))

    sub.add_parser("config", help="print the effective configuration as JSON")
    return parser


def cmd_serve(settings: Settings, service: str) -> int:
    import uvicorn

    from voiced.logging import configure_logging

    configure_logging(service, settings.server.log_level, settings.server.log_dir)
    if service == "stt":
        from voiced.stt import build_stt_app

        app = build_stt_app(settings)
        port = settings.stt.http_port
    else:
        from voiced.tts import build_tts_app

        app = build_tts_app(settings)
        port = settings.tts.http_port
    uvicorn.run(app, host=settings.server.bind, port=port, log_config=None)
    return 0


def cmd_config(settings: Settings) -> int:
    payload = settings.model_dump(mode="json")
    payload["_config_file"] = str(config_path())
    print(json.dumps(payload, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in LIFECYCLE:
        print(f"voiced {args.command}: not implemented yet", file=sys.stderr)
        return 2
    settings = load_settings()
    if args.command == "serve":
        return cmd_serve(settings, args.service)
    if args.command == "config":
        return cmd_config(settings)
    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":
    sys.exit(main())
