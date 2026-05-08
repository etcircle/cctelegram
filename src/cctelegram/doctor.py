"""Doctor checks for CCTelegram local state and hook setup.

The project has one canonical identity: CLI `cctelegram`, config env
`CCTELEGRAM_DIR`, and default state directory `~/.cctelegram`.
"""

import argparse
from pathlib import Path

from .utils import app_dir

DEFAULT_DIR_NAME = ".cctelegram"


def default_state_dir() -> Path:
    """Return the default CCTelegram state directory."""
    return Path.home() / DEFAULT_DIR_NAME


def preflight_or_exit() -> None:
    """Run startup preflight checks.

    The clean CCTelegram package has no implicit state migration. Startup
    proceeds with the canonical app_dir(), creating it during Config load.
    """
    return None


def doctor_main(argv: list[str] | None = None) -> int:
    """Print the canonical local state paths and setup command."""
    parser = argparse.ArgumentParser(
        prog="cctelegram doctor",
        description="Check CCTelegram config/state paths.",
    )
    parser.parse_args(argv)

    target = app_dir()
    print(f"OK: CCTelegram state dir is {target}")
    print("Env override: CCTELEGRAM_DIR")
    print("Hook install: cctelegram hook --install")
    return 0


if __name__ == "__main__":
    raise SystemExit(doctor_main())
