"""Runs first inside run_python's child process, then runs the model's code. Standard library only.

agent/sandbox.py starts it as `python -I -S sandbox_bootstrap.py '<config JSON>'`, with the run directory as the
working directory and the code on stdin. In order, it:

1. reads the code from stdin;
2. sets resource limits: address space, CPU seconds, largest file written, no core dumps;
3. confines the process with Landlock, when the kernel has it (Linux 5.13 and later): files under the read paths
   (the interpreter and system libraries) can be read, files under the write paths (the run directory) can be read
   and written, and nothing else can be opened; with Landlock ABI 4 or later, TCP connect and bind are refused; with
   ABI 6 or later, signals and abstract Unix sockets cannot reach processes outside;
4. reports what it applied on the first line of stderr (CONFINEMENT_MARKER), which the parent removes;
5. runs the code as __main__.

Landlock cannot be undone by the code it confines. Without it, only the working directory, the empty environment,
the resource limits and (when the parent could start one) the network namespace remain.
"""

import ctypes
import json
import linecache
import os
import resource
import sys
import traceback

CONFINEMENT_MARKER = "carousearch-sandbox:"

SYSCALL_LANDLOCK_CREATE_RULESET = 444  # the same number on every Linux architecture
SYSCALL_LANDLOCK_ADD_RULE = 445
SYSCALL_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
FILE_RIGHTS_MASK = ACCESS_EXECUTE | ACCESS_WRITE_FILE | ACCESS_READ_FILE | (1 << 14) | (1 << 15)  # + truncate, ioctl
NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1


class RulesetAttributes(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class PathBeneathAttributes(ctypes.Structure):
    # The kernel's struct is packed to 12 bytes and it reads exactly 12; these fields have the same offsets, and the
    # trailing padding ctypes adds is never read.
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def main() -> None:
    config = json.loads(sys.argv[1])
    code = sys.stdin.read()
    set_resource_limits(config)
    confinement = confine_with_landlock(config["read_paths"], config["write_paths"])
    print(f"{CONFINEMENT_MARKER} {confinement}", file=sys.stderr, flush=True)
    run_snippet(code)


def run_snippet(code: str) -> None:
    """Run the code as __main__; a traceback shows the snippet's own lines and none of this file's frames."""
    sys.argv = ["snippet.py"]
    linecache.cache["snippet.py"] = (len(code), None, code.splitlines(keepends=True), "snippet.py")
    try:
        exec(compile(code, "snippet.py", "exec"), {"__name__": "__main__"})  # noqa: S102 (the point of the sandbox)
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 (any error in the snippet is reported to the model)
        traceback.print_exception(type(error), error, error.__traceback__.tb_next)
        sys.exit(1)


def set_resource_limits(config: dict) -> None:
    memory_bytes = config["memory_megabytes"] * 1024 * 1024
    file_bytes = config["max_file_megabytes"] * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (config["cpu_seconds"], config["cpu_seconds"]))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def confine_with_landlock(read_paths: list[str], write_paths: list[str]) -> str:
    """Apply the rules; returns what was applied, e.g. "landlock abi 7: files, tcp, scopes"."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(SYSCALL_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), LANDLOCK_CREATE_RULESET_VERSION)
    if abi < 1:
        return "no landlock (files and network not confined by the kernel)"
    handled_fs = filesystem_rights(abi)
    attributes = RulesetAttributes(
        handled_access_fs=handled_fs,
        handled_access_net=(NET_BIND_TCP | NET_CONNECT_TCP) if abi >= 4 else 0,
        scoped=(SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL) if abi >= 6 else 0,
    )
    attributes_size = 8 if abi < 4 else 16 if abi < 6 else 24
    ruleset = libc.syscall(SYSCALL_LANDLOCK_CREATE_RULESET, ctypes.byref(attributes), attributes_size, 0)
    if ruleset < 0:
        return f"no landlock (creating the ruleset failed: {os.strerror(ctypes.get_errno())})"
    read_rights = ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_READ_DIR
    for path in read_paths:
        allow_path(libc, ruleset, path, read_rights & handled_fs)
    for path in write_paths:
        allow_path(libc, ruleset, path, handled_fs)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return "no landlock (no_new_privs refused)"
    if libc.syscall(SYSCALL_LANDLOCK_RESTRICT_SELF, ruleset, 0) != 0:
        return f"no landlock (restricting failed: {os.strerror(ctypes.get_errno())})"
    os.close(ruleset)
    applied = ["files"] + (["tcp"] if abi >= 4 else []) + (["scopes"] if abi >= 6 else [])
    return f"landlock abi {abi}: {', '.join(applied)}"


def filesystem_rights(abi: int) -> int:
    """Every filesystem right the kernel's Landlock ABI knows, so each one is denied unless a rule allows it."""
    rights = (1 << 13) - 1  # ABI 1: execute .. make_sym
    if abi >= 2:
        rights |= 1 << 13  # refer
    if abi >= 3:
        rights |= 1 << 14  # truncate
    if abi >= 5:
        rights |= 1 << 15  # ioctl_dev
    return rights


def allow_path(libc: ctypes.CDLL, ruleset: int, path: str, rights: int) -> None:
    """Allow `rights` beneath `path` (only the file rights for a file). A missing path is skipped."""
    try:
        descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return
    try:
        if not os.path.isdir(path):
            rights &= FILE_RIGHTS_MASK
        rule = PathBeneathAttributes(allowed_access=rights, parent_fd=descriptor)
        libc.syscall(SYSCALL_LANDLOCK_ADD_RULE, ruleset, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
