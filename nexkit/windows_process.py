"""Native Windows process trees; no consumer code runs before job assignment.

An anonymous Job Object owns the complete process tree. A trusted Python worker
waits for a controller gate before starting the accepted argv. Closing the job
kills descendants after success, timeout, cancellation or controller failure.
See https://learn.microsoft.com/windows/win32/procthread/job-objects.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes as w
from pathlib import Path


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class BASIC_LIMITS(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC_LIMITS),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class BASIC_ACCOUNTING(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", w.DWORD),
        ("TotalProcesses", w.DWORD),
        ("ActiveProcesses", w.DWORD),
        ("TotalTerminatedProcesses", w.DWORD),
    ]


def api():
    if os.name != "nt":
        raise OSError("Windows process support requires native Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, arguments, result in (
        ("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE),
        ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
        ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
        ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
        (
            "QueryInformationJobObject",
            [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID],
            w.BOOL,
        ),
        ("CloseHandle", [w.HANDLE], w.BOOL),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def reap(kernel, job):
    # TerminateJobObject starts termination; restoration needs proof that the
    # last descendant has actually exited before traversing writable files.
    checked(kernel.TerminateJobObject(job, 1))
    deadline = time.monotonic() + 10
    while True:
        accounting = BASIC_ACCOUNTING()
        checked(
            kernel.QueryInformationJobObject(
                job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            )
        )
        if accounting.ActiveProcesses == 0:
            return
        if time.monotonic() >= deadline:
            raise OSError("Windows command descendants did not exit before workspace restoration")
        time.sleep(0.01)


def execute(argv, root, seconds, env, log, *, stdin=None):
    """Return the real child exit code and timeout flag; always reap the tree."""
    kernel = api()
    job = checked(kernel.CreateJobObjectW(None, None))
    proc = None
    try:
        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        checked(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        with tempfile.TemporaryDirectory(prefix="nexkit-process-") as directory:
            root_control = Path(directory)
            request = root_control / "request.json"
            gate = root_control / "start"
            request.write_text(
                json.dumps({"argv": [str(x) for x in argv], "cwd": str(root)}),
                encoding="utf-8",
            )
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("windows_worker.py")),
                    str(request),
                    str(gate),
                ],
                cwd=directory,
                env=env,
                stdin=subprocess.DEVNULL if stdin is None else stdin,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            # The worker waits without loading consumer modules or executing
            # the command. Failure to join the job never falls back to Popen.
            checked(kernel.AssignProcessToJobObject(job, int(proc._handle)))
            gate.touch()
            try:
                code, timed_out = proc.wait(timeout=seconds), False
            except subprocess.TimeoutExpired:
                code, timed_out = 124, True
            finally:
                reap(kernel, job)
                proc.wait(timeout=10)
            return code, timed_out
    finally:
        kernel.CloseHandle(job)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
