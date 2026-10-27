"""run_python's sandbox: a real subprocess in a temporary run directory. Confinement is asserted as far as this
kernel enforces it: Landlock when the bootstrap reports it, the working directory and environment always."""

import json
import os
from pathlib import Path

import pytest

from carousearch.agent.sandbox import (
    SandboxLimits,
    SandboxResult,
    detect_network_namespace_prefix,
    format_sandbox_output,
    interpreter_read_paths,
    keep_end,
    keep_start,
    next_snippet_number,
    run_python_in_sandbox,
    sandbox_command,
    sandbox_config,
    sandbox_environment,
    snippet_prelude,
    split_confinement_line,
)
from carousearch.agent.sandbox_bootstrap import CONFINEMENT_MARKER, filesystem_rights

QUICK = SandboxLimits(wall_seconds=10)


@pytest.fixture
def run_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "data" / "runs" / "20261027-000000-test"
    directory.mkdir(parents=True)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-must-stay-secret\n")
    return directory


def run(code: str, directory: Path, limits: SandboxLimits = QUICK) -> SandboxResult:
    return run_python_in_sandbox(code, directory, limits, network_prefix=())


def landlock_active(result: SandboxResult) -> bool:
    return result.confinement.startswith("landlock")


def test_a_snippet_runs_in_the_run_directory_with_the_standard_library(run_directory: Path) -> None:
    code = "import os, json, re, statistics, sqlite3\nprint(os.getcwd())\nprint(statistics.median([3, 1, 2]))"

    result = run(code, run_directory)

    assert (result.exit_code, result.timed_out) == (0, False)
    assert result.stdout.splitlines() == [str(run_directory), "2"]
    assert format_sandbox_output(result, 4000) == f"{run_directory}\n2"


def test_the_snippet_and_its_output_are_kept_in_the_run_directory(run_directory: Path) -> None:
    run("print('first')", run_directory)
    run("print('second')", run_directory)

    snippets = run_directory / "python"
    assert sorted(os.listdir(snippets)) == [
        "001.py", "001.stderr", "001.stdout", "002.py", "002.stderr", "002.stdout"
    ]
    assert (snippets / "002.py").read_text() == "print('second')"
    assert (snippets / "002.stdout").read_text() == "second\n"


def test_an_error_returns_stderr_with_the_snippets_own_traceback(run_directory: Path) -> None:
    result = run("x = 1\nprint('before')\n1 / 0", run_directory)

    output = format_sandbox_output(result, 4000)
    assert result.exit_code == 1
    assert output.startswith("before\nExit code 1.\nstderr:\nTraceback (most recent call last):")
    assert 'File "snippet.py", line 3' in output and "1 / 0" in output and "ZeroDivisionError" in output
    assert "sandbox_bootstrap" not in output


def test_a_snippet_that_runs_too_long_is_killed(run_directory: Path) -> None:
    result = run("import time\nprint('started', flush=True)\ntime.sleep(30)", run_directory,
                 SandboxLimits(wall_seconds=1))

    assert (result.exit_code, result.timed_out) == (None, True)
    assert format_sandbox_output(result, 4000) == "started\nStopped: the snippet ran past its time limit and was killed."


def test_long_output_is_cut(run_directory: Path) -> None:
    result = run("print('x' * 50_000)", run_directory, SandboxLimits(wall_seconds=10, max_output_characters=300))

    output = format_sandbox_output(result, 300)
    assert len(output) < 400
    assert output.startswith("x" * 300) and "more characters cut" in output


def test_the_environment_carries_no_secrets(run_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-parent")

    result = run("import os, json\nprint(json.dumps(dict(os.environ)))", run_directory)

    environment = json.loads(result.stdout)
    assert "OPENAI_API_KEY" not in environment
    assert environment["HOME"] == environment["TMPDIR"] == str(run_directory)


def test_the_parent_env_file_cannot_be_read_by_a_relative_path(run_directory: Path) -> None:
    code = "print(open('../../../.env').read())"

    result = run(code, run_directory)

    if not landlock_active(result):
        pytest.skip(f"this kernel does not confine files ({result.confinement}); see the sandbox module docstring")
    assert result.exit_code == 1
    assert "PermissionError" in result.stderr
    assert "sk-must-stay-secret" not in result.stdout + result.stderr


def test_files_outside_the_run_directory_cannot_be_written_or_listed(run_directory: Path) -> None:
    code = (
        "import os\n"
        "for action, call in [('write', lambda: open('../escaped.txt', 'w')), ('list', lambda: os.listdir('..'))]:\n"
        "    try:\n"
        "        call()\n"
        "        print(action, 'allowed')\n"
        "    except PermissionError:\n"
        "        print(action, 'refused')\n"
        "open('inside.txt', 'w').write('ok')\n"
        "print(open('inside.txt').read())\n"
    )

    result = run(code, run_directory)

    if not landlock_active(result):
        pytest.skip(f"this kernel does not confine files ({result.confinement})")
    assert result.stdout.splitlines() == ["write refused", "list refused", "ok"]
    assert not (run_directory.parent / "escaped.txt").exists()


def test_tcp_connections_are_refused(run_directory: Path) -> None:
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('connected')\n"
        "except OSError as error:\n"
        "    print('refused', type(error).__name__)\n"
    )

    result = run(code, run_directory)

    if "tcp" not in result.confinement:
        pytest.skip(f"this kernel's Landlock does not handle TCP ({result.confinement})")
    assert result.stdout.startswith("refused")


def test_memory_beyond_the_limit_fails_inside_the_snippet(run_directory: Path) -> None:
    result = run("block = bytearray(400 * 1024 * 1024)\nprint('allocated')", run_directory,
                 SandboxLimits(wall_seconds=10, memory_megabytes=256))

    assert result.exit_code == 1
    assert "MemoryError" in result.stderr


def test_the_network_namespace_is_used_when_the_machine_has_one(run_directory: Path) -> None:
    prefix = detect_network_namespace_prefix()
    if not prefix:
        pytest.skip("unshare cannot create a network namespace here")

    result = run_python_in_sandbox("print('inside')", run_directory, QUICK, network_prefix=prefix)

    assert result.stdout == "inside\n"
    assert result.confinement.endswith("; network namespace")


# --- pure parts ---------------------------------------------------------------------------------------------------


def test_the_command_runs_the_bootstrap_isolated_with_its_config() -> None:
    config = sandbox_config(Path("/runs/1"), ["/usr"], SandboxLimits(wall_seconds=20, memory_megabytes=512))

    command = sandbox_command("/usr/bin/python3", Path("/app/sandbox_bootstrap.py"), config, ("unshare", "-rn"))

    assert command[:6] == ["unshare", "-rn", "/usr/bin/python3", "-I", "-S", "/app/sandbox_bootstrap.py"]
    assert json.loads(command[6]) == {
        "read_paths": ["/usr"], "write_paths": ["/runs/1"], "memory_megabytes": 512, "cpu_seconds": 21,
        "max_file_megabytes": 64,
    }


def test_the_environment_is_built_from_nothing() -> None:
    assert sandbox_environment(Path("/runs/1")) == {
        "PATH": "/usr/bin:/bin", "HOME": "/runs/1", "TMPDIR": "/runs/1", "SQLITE_TMPDIR": "/runs/1",
        "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
    }


def test_the_read_paths_cover_the_standard_library_and_never_the_home_folder() -> None:
    paths = interpreter_read_paths(["/opt/site-packages"])

    assert "/usr" in paths and "/opt/site-packages" in paths
    assert not any(path in (str(Path.home()), str(Path.home() / "carousearch")) for path in paths)


def test_the_prelude_puts_optional_libraries_on_the_path_only_when_there_are_any() -> None:
    assert snippet_prelude([]) == ""
    assert snippet_prelude(["/x/site-packages"]) == "import sys\nsys.path.extend(['/x/site-packages'])\n"


def test_the_confinement_report_is_split_from_stderr() -> None:
    assert split_confinement_line(f"{CONFINEMENT_MARKER} landlock abi 7: files\nTraceback ...") == (
        "landlock abi 7: files", "Traceback ..."
    )
    assert split_confinement_line("Fatal Python error") == (None, "Fatal Python error")


def test_output_formatting() -> None:
    def result(stdout: str = "", stderr: str = "", exit_code: int | None = 0, timed_out: bool = False) -> SandboxResult:
        return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out, confinement="")

    assert format_sandbox_output(result(), 100) == "(no output)"
    assert format_sandbox_output(result("42\n"), 100) == "42"
    assert format_sandbox_output(result(stderr="boom", exit_code=2), 100) == "Exit code 2.\nstderr:\nboom"
    assert keep_start("abcdef", 3) == "abc\n[... 3 more characters cut]"
    assert keep_end("abcdef", 3) == "[... 3 characters cut ...]\ndef"


def test_snippet_numbers_follow_the_highest_one() -> None:
    assert next_snippet_number([]) == 1
    assert next_snippet_number(["001.py", "001.stdout", "007.py", "notes.txt"]) == 8


def test_every_filesystem_right_of_the_abi_is_handled() -> None:
    assert filesystem_rights(1) == (1 << 13) - 1
    assert filesystem_rights(3) == (1 << 15) - 1
    assert filesystem_rights(7) == (1 << 16) - 1
