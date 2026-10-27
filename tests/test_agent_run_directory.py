"""Run folders: named by the UTC start time, created under <data dir>/runs."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from carousearch.agent.run_directory import new_run_directory, run_directory_name


def test_run_folders_are_named_by_utc_start_time() -> None:
    started = datetime(2026, 10, 27, 10, 15, 0, tzinfo=timezone(timedelta(hours=8)))

    assert run_directory_name(started, "ab12") == "20261027-021500-ab12"


def test_a_new_run_folder_is_created_and_never_reused(tmp_path: Path) -> None:
    started = datetime(2026, 10, 27, tzinfo=UTC)

    created = new_run_directory(tmp_path, started, "x")

    assert created == tmp_path / "runs" / "20261027-000000-x" and created.is_dir()
    with pytest.raises(FileExistsError):
        new_run_directory(tmp_path, started, "x")
    assert new_run_directory(tmp_path, started).name.startswith("20261027-000000-")
