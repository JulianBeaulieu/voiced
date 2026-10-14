"""Argument parsing: every subcommand, every flag, one table."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voiced.cli import LIFECYCLE, build_parser

CASES: list[tuple[list[str], dict[str, Any]]] = [
    (["install"], {"dry_run": False, "venv": None, "python": None, "source": None}),
    (
        [
            "install",
            "--dry-run",
            "--venv",
            "/opt/v",
            "--python",
            "3.12",
            "--source",
            "voiced==1.0",
            "--user",
            "someone",
            "--staging-dir",
            "/tmp/stage",
            "--skip-models",
            "--no-uv",
        ],
        {
            "dry_run": True,
            "venv": Path("/opt/v"),
            "python": "3.12",
            "source": "voiced==1.0",
            "user": "someone",
            "staging_dir": Path("/tmp/stage"),
            "skip_models": True,
            "no_uv": True,
        },
    ),
    (["start"], {"service": "all"}),
    (["start", "stt"], {"service": "stt"}),
    (["stop", "watchdog"], {"service": "watchdog"}),
    (["status"], {"json": False}),
    (["status", "--json"], {"json": True}),
    (["logs"], {"service": "all", "lines": 50, "follow": False, "stderr": False}),
    (["logs", "tts", "-n", "10", "-f", "--stderr"], {"lines": 10, "follow": True, "stderr": True}),
    (["uninstall"], {"yes": False, "remove_config": False, "remove_models": False}),
    (
        ["uninstall", "--yes", "--remove-config", "--remove-models", "--remove-logs"],
        {"yes": True, "remove_config": True, "remove_models": True, "remove_logs": True},
    ),
    (["doctor"], {"json": False, "metal_probe": False}),
    (["doctor", "--json", "--metal-probe"], {"json": True, "metal_probe": True}),
    (["watchdog"], {"once": False}),
    (["watchdog", "--once"], {"once": True}),
    (["serve", "stt"], {"service": "stt"}),
    (["config"], {}),
]


@pytest.mark.parametrize(("argv", "expected"), CASES, ids=[" ".join(c[0]) for c in CASES])
def test_parses(argv: list[str], expected: dict[str, Any]) -> None:
    args = build_parser().parse_args(argv)
    assert args.command == argv[0]
    for key, value in expected.items():
        assert getattr(args, key) == value, key


def test_every_lifecycle_command_is_registered() -> None:
    parser = build_parser()
    for cmd in LIFECYCLE:
        assert parser.parse_args([cmd]).command == cmd


@pytest.mark.parametrize("argv", [["start", "bogus"], ["logs", "nope"], ["start", "--kill"]])
def test_rejects(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
