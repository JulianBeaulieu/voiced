"""Argument parsing for every subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest

from voiced.cli import LIFECYCLE, build_parser


def test_every_lifecycle_command_parses() -> None:
    parser = build_parser()
    for cmd in LIFECYCLE:
        assert parser.parse_args([cmd]).command == cmd


def test_install_options() -> None:
    args = build_parser().parse_args(
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
        ]
    )
    assert args.dry_run and args.skip_models and args.no_uv
    assert args.venv == Path("/opt/v")
    assert args.python == "3.12"
    assert args.source == "voiced==1.0"
    assert args.user == "someone"
    assert args.staging_dir == Path("/tmp/stage")


def test_install_defaults() -> None:
    args = build_parser().parse_args(["install"])
    assert args.venv is None and args.python is None and args.source is None
    assert not args.dry_run


@pytest.mark.parametrize("cmd", ["start", "stop", "logs"])
def test_service_argument(cmd: str) -> None:
    parser = build_parser()
    assert parser.parse_args([cmd]).service == "all"
    assert parser.parse_args([cmd, "stt"]).service == "stt"
    assert parser.parse_args([cmd, "watchdog"]).service == "watchdog"
    with pytest.raises(SystemExit):
        parser.parse_args([cmd, "bogus"])


def test_start_kill_flag() -> None:
    assert build_parser().parse_args(["start", "tts", "--kill"]).kill is True


def test_status_and_doctor_json() -> None:
    parser = build_parser()
    assert parser.parse_args(["status", "--json"]).json is True
    assert parser.parse_args(["doctor", "--json"]).json is True
    assert parser.parse_args(["doctor", "--metal-probe"]).metal_probe is True


def test_logs_options() -> None:
    args = build_parser().parse_args(["logs", "stt", "-n", "10", "-f", "--stderr"])
    assert args.lines == 10 and args.follow and args.stderr
    assert build_parser().parse_args(["logs"]).lines == 50


def test_uninstall_flags() -> None:
    args = build_parser().parse_args(
        ["uninstall", "--yes", "--remove-config", "--remove-models", "--remove-logs", "--force"]
    )
    assert args.yes and args.remove_config and args.remove_models and args.remove_logs
    assert args.force
    plain = build_parser().parse_args(["uninstall"])
    assert not plain.yes and not plain.remove_config


def test_watchdog_once() -> None:
    assert build_parser().parse_args(["watchdog", "--once"]).once is True
    assert build_parser().parse_args(["watchdog"]).once is False


def test_serve_and_config_still_parse() -> None:
    parser = build_parser()
    assert parser.parse_args(["serve", "stt"]).service == "stt"
    assert parser.parse_args(["config"]).command == "config"
