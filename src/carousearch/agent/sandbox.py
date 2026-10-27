"""run_python's sandbox: runs a Python snippet in a separate, confined process inside the run directory.

How it is confined (agent/sandbox_bootstrap.py does the in-process part):
- `python -I -S`: no site-packages, no PYTHON* environment variables, no user directory; an empty environment
  (no API keys) with HOME and TMPDIR pointing at the run directory;
- the run directory is the working directory; with Landlock (Linux 5.13 and later) the process can read only the
  interpreter and system libraries and write only under the run directory, and TCP connections are refused;
- `unshare -rn` (an unprivileged user and network namespace) when the machine allows it, so not even UDP leaves;
- resource limits: wall clock (the parent kills the process group), CPU seconds, address space, file size;
- stdout and stderr go to files in the run directory and only their first (stdout) or last (stderr) few
  thousand characters come back.

Pure: the command, the environment, the configuration and the output formatting. I/O: run_python_in_sandbox
(writes the snippet and runs the process) and detect_network_namespace_prefix (runs `unshare` once).
"""

import json
import logging
import os
import signal
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib.util import find_spec
from pathlib import Path

from carousearch.agent.sandbox_bootstrap import CONFINEMENT_MARKER

logger = logging.getLogger(__name__)

BOOTSTRAP_FILE = Path(__file__).with_name("sandbox_bootstrap.py")
SNIPPETS_DIRECTORY_NAME = "python"
SYSTEM_READ_PATHS = ("/usr", "/lib", "/lib64", "/bin", "/etc/ld.so.cache", "/etc/localtime", "/dev/null", "/dev/urandom")
NETWORK_NAMESPACE_PREFIXES = (("unshare", "-rn"), ("unshare", "-n"))
OPTIONAL_LIBRARIES = ("pandas", "numpy")  # put on the snippet's path when installed; never installed for this


@dataclass(frozen=True)
class SandboxLimits:
    wall_seconds: float = 20.0
    memory_megabytes: int = 512
    max_file_megabytes: int = 64
    max_output_characters: int = 4000  # stdout and stderr together, as the model sees them


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None  # None when the process was killed for taking too long
    stdout: str
    stderr: str
    timed_out: bool
    confinement: str  # what the bootstrap reported, plus the network namespace, e.g. "landlock abi 7: files, tcp; ..."


DEFAULT_SANDBOX_LIMITS = SandboxLimits()


# --- building the run (pure) ------------------------------------------------------------------------------------


def sandbox_config(working_directory: Path, read_paths: Sequence[str], limits: SandboxLimits) -> dict:
    """The JSON configuration the bootstrap reads from its first argument."""
    return {
        "read_paths": list(read_paths),
        "write_paths": [str(working_directory)],
        "memory_megabytes": limits.memory_megabytes,
        "cpu_seconds": int(limits.wall_seconds) + 1,
        "max_file_megabytes": limits.max_file_megabytes,
    }


def sandbox_command(
    python_executable: str, bootstrap_file: Path, config: dict, network_prefix: Sequence[str]
) -> list[str]:
    return [*network_prefix, python_executable, "-I", "-S", str(bootstrap_file), json.dumps(config)]


def sandbox_environment(working_directory: Path) -> dict[str, str]:
    """A fresh environment: nothing of the parent's (so no API keys), temporary files in the run directory."""
    directory = str(working_directory)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": directory,
        "TMPDIR": directory,
        "SQLITE_TMPDIR": directory,
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }


def interpreter_read_paths(optional_library_paths: Sequence[str] = ()) -> list[str]:
    """System libraries plus this interpreter's standard library (which may live outside /usr, e.g. under uv)."""
    paths = dict.fromkeys(SYSTEM_READ_PATHS)
    for name in ("stdlib", "platstdlib"):
        paths[sysconfig.get_paths()[name]] = None
    library_directory = sysconfig.get_config_var("LIBDIR")
    if library_directory:
        paths[library_directory] = None
    paths.update(dict.fromkeys(optional_library_paths))
    return list(paths)


def snippet_prelude(library_paths: Sequence[str]) -> str:
    """Lines put before the model's code so installed optional libraries (pandas) can be imported despite -S."""
    if not library_paths:
        return ""
    return f"import sys\nsys.path.extend({list(library_paths)!r})\n"


def split_confinement_line(stderr: str) -> tuple[str | None, str]:
    """The bootstrap's report on the first stderr line, and the rest of stderr."""
    first_line, _, rest = stderr.partition("\n")
    if first_line.startswith(CONFINEMENT_MARKER):
        return first_line.removeprefix(CONFINEMENT_MARKER).strip(), rest
    return None, stderr


def format_sandbox_output(result: SandboxResult, max_characters: int) -> str:
    """What the model reads: stdout (its start), then on failure the exit status and stderr (its end)."""
    half = max_characters // 2
    stdout = keep_start(result.stdout, max_characters if not result.stderr.strip() else half)
    if result.timed_out:
        status = "Stopped: the snippet ran past its time limit and was killed."
    elif result.exit_code == 0:
        status = ""
    else:
        status = f"Exit code {result.exit_code}."
    sections = [stdout.rstrip() if stdout.strip() else ("(no output)" if not status else "")]
    if status:
        sections.append(status)
    if result.stderr.strip():
        sections.append("stderr:\n" + keep_end(result.stderr, half))
    return "\n".join(section for section in sections if section).strip()


def keep_start(text: str, max_characters: int) -> str:
    if len(text) <= max_characters:
        return text
    return text[:max_characters] + f"\n[... {len(text) - max_characters:,} more characters cut]"


def keep_end(text: str, max_characters: int) -> str:
    if len(text) <= max_characters:
        return text
    return f"[... {len(text) - max_characters:,} characters cut ...]\n" + text[-max_characters:]


def next_snippet_number(existing_names: Sequence[str]) -> int:
    """1 for an empty directory; one more than the highest NNN of NNN.py otherwise."""
    numbers = [int(name[:-3]) for name in existing_names if name.endswith(".py") and name[:-3].isdigit()]
    return max(numbers, default=0) + 1


# --- running it (I/O) -------------------------------------------------------------------------------------------


def run_python_in_sandbox(
    code: str,
    working_directory: Path,
    limits: SandboxLimits = DEFAULT_SANDBOX_LIMITS,
    network_prefix: Sequence[str] | None = None,
) -> SandboxResult:
    """Run `code` once in the sandbox. The snippet and its output stay in <working directory>/python/NNN.*.
    `network_prefix` is the command that starts a network namespace; None detects one."""
    working_directory = working_directory.resolve()
    snippets = working_directory / SNIPPETS_DIRECTORY_NAME
    snippets.mkdir(parents=True, exist_ok=True)
    number = next_snippet_number(os.listdir(snippets))
    library_paths = optional_library_paths()
    script = snippet_prelude(library_paths) + code
    (snippets / f"{number:03d}.py").write_text(code, encoding="utf-8")
    prefix = detect_network_namespace_prefix() if network_prefix is None else tuple(network_prefix)
    config = sandbox_config(working_directory, interpreter_read_paths(library_paths), limits)
    command = sandbox_command(sys.executable, BOOTSTRAP_FILE, config, prefix)
    stdout_path, stderr_path = snippets / f"{number:03d}.stdout", snippets / f"{number:03d}.stderr"
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        exit_code, timed_out = run_process(
            command, script, working_directory, stdout_file, stderr_file, limits.wall_seconds
        )
    confinement, stderr = split_confinement_line(read_limited(stderr_path, limits.max_output_characters * 4))
    network = "network namespace" if prefix else "no network namespace"
    return SandboxResult(
        exit_code=exit_code,
        stdout=read_limited(stdout_path, limits.max_output_characters * 4),
        stderr=stderr,
        timed_out=timed_out,
        confinement=f"{confinement or 'no report from the bootstrap'}; {network}",
    )


def run_process(
    command: list[str], script: str, working_directory: Path, stdout_file, stderr_file, wall_seconds: float
) -> tuple[int | None, bool]:
    """(exit code, timed out). The process gets its own session so a timeout kills it and its children."""
    process = subprocess.Popen(
        command,
        cwd=working_directory,
        env=sandbox_environment(working_directory),
        stdin=subprocess.PIPE,
        stdout=stdout_file,
        stderr=stderr_file,
        start_new_session=True,
    )
    try:
        process.communicate(script.encode("utf-8"), timeout=wall_seconds)
    except subprocess.TimeoutExpired:
        kill_process_group(process)
        process.wait()
        return None, True
    return process.returncode, False


def kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def read_limited(path: Path, max_characters: int) -> str:
    """The start and end of a possibly huge output file, never more than about `max_characters`."""
    size = path.stat().st_size
    with path.open("rb") as file:
        if size <= max_characters:
            return file.read().decode("utf-8", errors="replace")
        start = file.read(max_characters // 2).decode("utf-8", errors="replace")
        file.seek(size - max_characters // 2)
        end = file.read().decode("utf-8", errors="replace")
    return f"{start}\n[... {size - max_characters:,} bytes cut ...]\n{end}"


@cache
def detect_network_namespace_prefix() -> tuple[str, ...]:
    """The first of `unshare -rn` and `unshare -n` that works on this machine, or () when neither does."""
    for prefix in NETWORK_NAMESPACE_PREFIXES:
        try:
            completed = subprocess.run([*prefix, "true"], capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            return prefix
    logger.warning("run_python: no network namespace available (unshare failed); Landlock alone blocks TCP")
    return ()


@cache
def optional_library_paths() -> tuple[str, ...]:
    """The site-packages folders holding pandas or numpy, when installed (looked up once per process)."""
    paths: dict[str, None] = {}
    for name in OPTIONAL_LIBRARIES:
        spec = find_spec(name)
        if spec is not None and spec.origin:
            paths[str(Path(spec.origin).parent.parent)] = None
    return tuple(paths)


@cache
def available_optional_libraries() -> tuple[str, ...]:
    """The names of OPTIONAL_LIBRARIES that are installed, e.g. ("pandas", "numpy"), or ()."""
    return tuple(name for name in OPTIONAL_LIBRARIES if find_spec(name) is not None)
