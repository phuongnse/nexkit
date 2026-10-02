"""The environment contract for consumer commands on Linux and Windows."""

from __future__ import annotations

import os
from pathlib import Path


def environment(home):
    # PATH is accepted toolchain configuration. Never inherit credentials,
    # Python startup hooks, shell profiles or per-user package configuration.
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "WINDIR",
        "PATHEXT",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    root = Path(home)
    temporary = root
    env.update(
        {
            "CI": "true",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "NO_COLOR": "1",
            "HOME": str(root),
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
        }
    )
    if os.name == "nt":
        for name in ("AppData/Roaming", "AppData/Local"):
            (root / name).mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "USERPROFILE": str(root),
                "APPDATA": str(root / "AppData/Roaming"),
                "LOCALAPPDATA": str(root / "AppData/Local"),
                "HOMEDRIVE": root.drive,
                "HOMEPATH": str(root)[len(root.drive) :],
            }
        )
    return env
