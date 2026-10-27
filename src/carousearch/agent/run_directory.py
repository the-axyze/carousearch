"""Where one agent run keeps its files: <data dir>/runs/<YYYYMMDD-HHMMSS>-<suffix>/, holding listings.db,
listings.json, photos/ and python/ (each run_python snippet and its output). Nothing removes old run folders.

run_directory_name is pure; new_run_directory creates the folder.
"""

import secrets
from datetime import UTC, datetime
from pathlib import Path

RUNS_DIRECTORY_NAME = "runs"


def run_directory_name(started_at: datetime, suffix: str) -> str:
    """The folder name, e.g. 20261027-021500-ab12cd for a run started at 02:15:00 UTC."""
    return f"{started_at.astimezone(UTC):%Y%m%d-%H%M%S}-{suffix}"


def new_run_directory(data_dir: Path, started_at: datetime, suffix: str | None = None) -> Path:
    """Create and return a new run folder; `suffix` defaults to a few random hex digits."""
    path = data_dir / RUNS_DIRECTORY_NAME / run_directory_name(started_at, suffix or secrets.token_hex(3))
    path.mkdir(parents=True)
    return path
