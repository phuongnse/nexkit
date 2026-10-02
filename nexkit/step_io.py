"""Transfer project-step data using the command workspace's execution account."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

INPUT = ".nexkit-step-input.json"
RESULT = ".nexkit-step-result.json"
MAX_RESULT_BYTES = 48000
RESULT_ERROR = "Project result must be a regular JSON file of at most 48 KB"


def _exists(directory, name):
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _perform_posix(operation, root, value=None):
    # Open the directory itself without following a replacement symlink. Keep
    # all reserved-file operations relative to this one directory descriptor.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if operation == "prepare":
            if any(_exists(directory, name) for name in (INPUT, RESULT)):
                raise ValueError("Reserved step input/result paths must not exist before execution")
            descriptor = os.open(
                INPUT,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(value, target, ensure_ascii=False, sort_keys=True)
                target.write("\n")
            return None
        if operation == "result-exists":
            return _exists(directory, RESULT)
        if operation != "read-result":
            raise ValueError("Unknown project-step file operation")
        try:
            descriptor = os.open(
                RESULT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return {"exists": False}
        except OSError as exc:
            raise ValueError(f"{RESULT_ERROR}: {exc}") from exc
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_RESULT_BYTES
            ):
                raise ValueError(RESULT_ERROR)
            data = source.read(MAX_RESULT_BYTES + 1)
            if len(data) > MAX_RESULT_BYTES:
                raise ValueError(RESULT_ERROR)
        return {"exists": True, "value": json.loads(data)}
    finally:
        os.close(directory)


def _perform_windows(operation, root, value=None):
    """Pin directories and open reserved files without following reparse points.

    Windows does not implement Python's dir_fd/O_NOFOLLOW contract. Handles
    opened without FILE_SHARE_DELETE prevent directory substitution while the
    operation is in progress. File handles are checked before reading bytes.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes as w

    class FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("attributes", w.DWORD),
            ("created", w.FILETIME),
            ("accessed", w.FILETIME),
            ("written", w.FILETIME),
            ("volume", w.DWORD),
            ("size_high", w.DWORD),
            ("size_low", w.DWORD),
            ("links", w.DWORD),
            ("index_high", w.DWORD),
            ("index_low", w.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(FILE_INFO)]
    kernel.GetFileInformationByHandle.restype = w.BOOL
    kernel.GetFileType.argtypes, kernel.GetFileType.restype = [w.HANDLE], w.DWORD
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    invalid = ctypes.c_void_p(-1).value
    directory_handles = []

    def opened(path, *, access=0, disposition=3, share=3):
        handle = kernel.CreateFileW(
            str(path),
            access,
            share,
            None,
            disposition,
            0x00200000 | 0x02000000,
            None,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        )
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def info(handle):
        result = FILE_INFO()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def exists(path):
        try:
            handle = opened(path)
        except FileNotFoundError:
            return False
        kernel.CloseHandle(handle)
        return True

    root = Path(root).absolute()
    if root.drive.startswith("\\\\"):
        raise ValueError("Project command workspaces must be on a local filesystem")
    try:
        for directory in (*reversed(root.parents), root):
            handle = opened(directory)
            directory_handles.append(handle)
            attributes = info(handle).attributes
            if not attributes & 0x10 or attributes & 0x400:
                raise ValueError("Project workspace uses a reparse point or is not a directory")
        if operation == "prepare":
            if any(exists(root / name) for name in (INPUT, RESULT)):
                raise ValueError("Reserved step input/result paths must not exist before execution")
            handle = opened(root / INPUT, access=0x40000000, disposition=1, share=0)
            try:
                descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
            except BaseException:
                kernel.CloseHandle(handle)
                raise
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                json.dump(value, target, ensure_ascii=False, sort_keys=True)
                target.write("\n")
            return None
        if operation == "result-exists":
            return exists(root / RESULT)
        if operation != "read-result":
            raise ValueError("Unknown project-step file operation")
        try:
            handle = opened(root / RESULT, access=0x80000000, share=1)
        except FileNotFoundError:
            return {"exists": False}
        try:
            details = info(handle)
            if (
                kernel.GetFileType(handle) != 1
                or details.attributes & (0x10 | 0x400)
                or details.links != 1
                or details.size_high
                or details.size_low > MAX_RESULT_BYTES
            ):
                raise ValueError(RESULT_ERROR)
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(descriptor, "rb") as source:
            data = source.read(MAX_RESULT_BYTES + 1)
            if len(data) > MAX_RESULT_BYTES:
                raise ValueError(RESULT_ERROR)
        return {"exists": True, "value": json.loads(data)}
    finally:
        for handle in reversed(directory_handles):
            kernel.CloseHandle(handle)


def _perform(operation, root, value=None):
    if os.name == "nt":
        return _perform_windows(operation, root, value)
    return _perform_posix(operation, root, value)


def transfer(operation, root, value=None):
    """Keep the controller outside the workspace and exchange only JSON data."""
    from .common import Blocked, canonical, run

    user = os.environ.get("NEXKIT_EXEC_USER")
    if user and user != "nexkit-agent":
        raise Blocked("Invalid isolated execution user")
    try:
        if not user:
            return _perform(operation, root, value)
        if os.name != "posix":
            raise Blocked("Managed project steps require the Linux container runtime")
        # The helper imports only the standard library. Isolated Python mode,
        # a clean environment and a trusted script path exclude consumer code
        # and the controller's credentials from file preparation/collection.
        result = run(
            [
                "sudo",
                "-n",
                "-u",
                user,
                "--",
                "env",
                "-i",
                "PATH=/usr/bin:/bin",
                sys.executable,
                "-I",
                os.environ.get("NEXKIT_STEP_HELPER", str(Path(__file__).resolve())),
                operation,
                str(root),
            ],
            cwd="/",
            data=canonical(value) if operation == "prepare" else "",
            env={"PATH": "/usr/bin:/bin"},
            timeout=10,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError) as exc:
        raise Blocked(f"Project step file transfer failed: {exc}") from exc


def main():
    operation, root = sys.argv[1:]
    # Isolated Python ignores PYTHONUTF8/PYTHONIOENCODING. Exchange explicit
    # UTF-8 bytes so the Windows code page cannot corrupt JSON on stdin/stdout.
    value = json.loads(sys.stdin.buffer.read().decode("utf-8")) if operation == "prepare" else None
    result = _perform(operation, root, value)
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Project step file transfer failed: {exc}") from exc
