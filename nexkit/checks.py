"""Execute real consumer commands and inspect native test reports."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path

from .common import Blocked, consumer_path
from .policy import require


def execute(argv, root, seconds, *, command_home=None):
    started = time.monotonic()
    # CI invokes this inside its own unprivileged job/container. Local runs do
    # not inherit the caller's provider/GitHub credentials or shell hooks.
    allowed = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env.update({"CI": "true", "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"})
    home_context = (
        nullcontext(command_home)
        if command_home
        else tempfile.TemporaryDirectory(prefix="nexkit-command-")
    )
    with home_context as home:
        env["HOME"] = home
        child_argv = argv
        if os.environ.get("NEXKIT_EXEC_USER"):
            require(
                os.environ["NEXKIT_EXEC_USER"] == "nexkit-agent", "Invalid isolated execution user"
            )
            if str(home) != "/home/nexkit-agent":
                os.chmod(home, 0o777)
            child_argv = [
                "sudo",
                "-n",
                "-u",
                "nexkit-agent",
                "--",
                "env",
                "-i",
                *[f"{k}={v}" for k, v in env.items()],
                *argv,
            ]
        with tempfile.TemporaryFile(mode="w+") as log:
            try:
                proc = subprocess.Popen(
                    child_argv,
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                return {"exit_code": 127, "log": str(exc), "seconds": 0, "timed_out": False}
            timed_out = False
            try:
                code = proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                code = 124
            finally:
                # Also clean up a background server started by a successful check.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if os.environ.get("NEXKIT_EXEC_USER"):
                    # sudo creates another process group. Kill the dedicated
                    # ephemeral job account's descendants, including setsid
                    # children, using the runner's privilege. Never do this to
                    # the local user's account.
                    subprocess.run(
                        ["sudo", "-n", "pkill", "-KILL", "-u", "nexkit-agent"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                proc.wait()
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - 24000))
            output = log.read()
    return {
        "exit_code": code,
        "log": output,
        "seconds": round(time.monotonic() - started, 3),
        "timed_out": timed_out,
    }


def test_count(report, root, output):
    fmt = report["format"]
    if fmt == "junit":
        path = Path(root, report["path"])
        require(path.resolve().is_relative_to(Path(root).resolve()), "Report escaped the workspace")
        require(path.is_file() and not path.is_symlink(), "JUnit report was not produced")
        require(path.stat().st_size <= 5_000_000, "JUnit report is too large")
        try:
            xml = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            raise Blocked(f"Invalid JUnit report: {exc}") from exc
        cases = list(xml.iter("testcase"))
        require(cases, "JUnit contains no test cases")
        require(
            not any(c.find("failure") is not None or c.find("error") is not None for c in cases),
            "JUnit contains failed test cases",
        )
        executed = sum(c.find("skipped") is None for c in cases)
        require(executed == len(cases), "Required test cases were skipped")
        return executed
    if fmt == "unittest":
        match = re.search(r"(?m)^Ran (\d+) tests? in [0-9.]+s\s*$", output)
        require(
            match is not None and re.search(r"(?m)^OK\s*$", output),
            "Missing successful unittest summary; skipped tests do not pass",
        )
        return int(match[1])
    if fmt == "tap":
        # Node's native TAP reporter includes an aggregate summary. Generic TAP
        # needs an explicit plan and one result for each top-level test.
        counts = re.findall(r"(?m)^# tests (\d+)\s*$", output)
        if counts:
            require(
                not re.search(r"(?m)^# (?:fail|cancelled|skipped|todo) [1-9]\d*\s*$", output),
                "TAP contains failed/cancelled/skipped/todo tests",
            )
            passes = re.findall(r"(?m)^# pass (\d+)\s*$", output)
            require(
                passes and int(passes[-1]) == int(counts[-1]),
                "TAP pass count does not match test count",
            )
            return int(counts[-1])
        require(
            not re.search(r"(?m)^not ok\b|#\s*(?:SKIP|TODO)\b", output, re.I),
            "TAP contains failing or skipped tests",
        )
        plan = re.search(r"(?m)^1\.\.(\d+)\s*$", output)
        cases = re.findall(r"(?m)^ok\s+\d+\b", output)
        require(plan is not None and int(plan[1]) == len(cases), "Incomplete TAP plan")
        return len(cases)
    raise Blocked("Unknown test report format")


def verify(cfg, root, candidate=None, *, setup=True, command_home=None, check_names=None):
    if command_home is None:
        with tempfile.TemporaryDirectory(prefix="nexkit-verification-") as home:
            return verify(
                cfg, root, candidate, setup=setup, command_home=home, check_names=check_names
            )
    checks = cfg["checks"]
    if check_names is not None:
        require(
            isinstance(check_names, list)
            and check_names
            and len(check_names) == len(set(check_names)),
            "Choose distinct configured check names",
        )
        require(set(check_names) <= {check["name"] for check in checks}, "Unknown configured check")
        checks = [check for check in checks if check["name"] in check_names]
    results = []
    if not checks:
        return {
            "candidate": candidate,
            "passed": False,
            "checks": [],
            "reason": "Pipeline configured; application verification commands do not exist yet",
        }
    if setup:
        for argv in cfg["environment"]["setup"]:
            result = execute(
                argv, root, cfg["limits"]["command_seconds"], command_home=command_home
            )
            if result["exit_code"]:
                return {
                    "candidate": candidate,
                    "passed": False,
                    "checks": [
                        {
                            "name": check["name"],
                            "kind": check["kind"],
                            "passed": False,
                            "executed": False,
                            "reason": "Environment setup failed",
                            "setup_failure": result,
                        }
                        for check in checks
                    ]
                    if check_names is not None
                    else [],
                    "reason": "Environment setup failed",
                    "setup_failure": result,
                }
    for check in checks:
        report = check.get("report")
        if report and report["format"] == "junit":
            path = consumer_path(root, report["path"])
            if path.exists() or path.is_symlink():
                require(not path.is_symlink(), "Refusing a symlink report path")
                path.unlink()  # A stale report can never establish this invocation.
        result = {
            "name": check["name"],
            "kind": check["kind"],
            **execute(check["argv"], root, check["timeout_seconds"], command_home=command_home),
        }
        result["passed"] = result["exit_code"] == 0 and not result["timed_out"]
        if result["passed"] and report:
            try:
                count = test_count(report, root, result["log"])
                require(count > 0, "No test cases actually executed")
                result["tests"] = count
            except Blocked as exc:
                result.update(passed=False, reason=str(exc))
        results.append(result)
    kinds = {r["kind"] for r in results if r["passed"]}
    return {
        "candidate": candidate,
        "passed": bool(results)
        and all(r["passed"] for r in results)
        and (check_names is not None or {"test", "e2e"} <= kinds),
        "checks": results,
    }


def complete_checks(cfg, results):
    """Each configured command must appear exactly once with its declared kind."""
    require(isinstance(results, list), "Missing check results")
    expected = {check["name"]: check["kind"] for check in cfg["checks"]}
    require(all(isinstance(item, dict) for item in results), "Invalid check record")
    names = [item.get("name") for item in results]
    require(all(isinstance(name, str) for name in names), "Missing check identity")
    require(
        len(names) == len(set(names)) and set(names) == set(expected),
        "Missing, duplicate or unexpected configured checks",
    )
    require(
        all(item.get("kind") == expected[item["name"]] for item in results),
        "Check kind differs from the accepted configuration",
    )


def combine_checks(context, reports):
    """Combine native job results; this function does not run or schedule jobs."""
    results = []
    for report in reports:
        require(
            isinstance(report, dict) and report.get("candidate") == context["candidate"],
            "Check report belongs to another candidate",
        )
        producer = report.get("producer", {})
        require(isinstance(producer, dict), "Invalid check producer")
        require(
            producer.get("run_key") == context["run_key"],
            "Check report belongs to another run attempt",
        )
        items = report.get("checks")
        require(
            isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict),
            "Each check job must report exactly one configured command",
        )
        require(
            producer.get("check") == items[0].get("name"),
            "Check producer identity differs from its result",
        )
        # Keep failures as feedback. Completeness is independent of success.
        require(
            type(report.get("passed")) is bool
            and report["passed"] == (items[0].get("passed") is True),
            "Check report contradicts its command result",
        )
        results.extend(items)
    complete_checks(context["config"], results)
    return {
        "candidate": context["candidate"],
        "checks": results,
        "passed": bool(results) and all(item.get("passed") is True for item in results),
    }
