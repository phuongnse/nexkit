"""Make the controller's Python usable after sudo removes loader variables."""

from __future__ import annotations

import os
import sys
import sysconfig
import tempfile
from pathlib import Path

from .common import Blocked, run
from .policy import require


def probe():
    return run(
        [
            "sudo",
            "-n",
            "env",
            "-i",
            "PATH=/usr/bin:/bin",
            sys.executable,
            "-I",
            "-c",
            "import pathlib, sys; print(repr((sys.executable, sys.version, sys.prefix, sys.base_prefix)))",
        ],
        check=False,
    )


def matches(result):
    expected = repr((sys.executable, sys.version, sys.prefix, sys.base_prefix))
    return result.returncode == 0 and result.stdout.strip() == expected


def prepare():
    """Register a shared runtime with the system loader, then test the real binary.

    Run before consumer setup. Never carry LD_LIBRARY_PATH or LD_PRELOAD into a
    privileged process. Managed jobs use Linux on both Linux and Windows hosts.
    """
    require(sys.platform == "linux", "Managed Python runs in Linux; Windows hosts use WSL 2")
    require(sys.version_info >= (3, 11), "The controller requires Python 3.11 or newer")
    result = probe()
    if not matches(result):
        require(
            sysconfig.get_config_var("Py_ENABLE_SHARED")
            and (result.returncode == 0 or "libpython" in result.stderr),
            "Controller Python failed its clean sudo probe: " + result.stderr[-2000:],
        )
        name = sysconfig.get_config_var("LDLIBRARY")
        require(
            isinstance(name, str) and name and Path(name).name == name,
            "Python must identify its shared library",
        )
        # setup-python relocates its build into RUNNER_TOOL_CACHE. LIBDIR can
        # still name the original build prefix, including a different runtime.
        directories = [Path(sys.base_prefix) / "lib"]
        directory = sysconfig.get_config_var("LIBDIR")
        if isinstance(directory, str) and Path(directory).is_absolute():
            directories.append(Path(directory))
        library = next((path.resolve() for path in directories if (path / name).is_file()), None)
        require(
            library is not None and "\n" not in str(library) and "\r" not in str(library),
            "The selected Python has no usable shared-library directory",
        )
        shared = (library / name).resolve()
        require(
            shared.is_file(),
            "The selected Python shared library is absent",
        )
        # This is the trusted runtime installed by setup-python, before a
        # consumer account exists. Reject directories writable by that account.
        for path in {library, *library.parents, shared, *shared.parents}:
            info = path.stat()
            require(
                info.st_uid in {0, os.getuid()} and not info.st_mode & 0o022,
                "Python shared libraries must be owned by the controller and protected from writes",
            )
        with tempfile.TemporaryDirectory(prefix="nexkit-python-") as temporary:
            source = Path(temporary) / "loader.conf"
            source.write_text(str(library) + "\n", encoding="utf-8")
            run(
                [
                    "sudo",
                    "-n",
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "644",
                    source,
                    "/etc/ld.so.conf.d/nexkit-python.conf",
                ]
            )
            run(["sudo", "-n", "/sbin/ldconfig"])
        result = probe()
    require(
        matches(result),
        "Controller Python must use the same interpreter and libraries without loader variables: "
        + result.stderr[-2000:],
    )
    return {"python": sys.executable, "version": sys.version, "sudo_clean_environment": True}


if __name__ == "__main__":
    try:
        print(prepare())
    except (Blocked, OSError) as exc:
        raise SystemExit(f"Controller Python runtime blocked: {exc}") from exc
