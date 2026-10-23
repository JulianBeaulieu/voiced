"""The ``voiced`` console script.

Redirects stdout and stderr to the service's log files when launchd started
it (``voiced.launchd.stdio``) before importing the CLI, so a traceback from
a failing import still lands in ``<service>.stderr.log``.
"""

from __future__ import annotations

from voiced.launchd.stdio import redirect_stdio_from_env


def main() -> int:
    redirect_stdio_from_env()
    from voiced.cli import main as cli_main

    return cli_main()
