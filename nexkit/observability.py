"""Bounded public activity records. These diagnostics never authorize delivery."""

from __future__ import annotations

import html
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from .common import canonical, digest, is_link, read_regular_bytes, write_json
from .pipelines import pipeline_id
from .policy import REPO, now, require

MAX_TRACE_BYTES = 8_000_000
MAX_LINE_BYTES = 128_000
MAX_EVENTS = 10_000
HEARTBEAT_SECONDS = 30
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
TOKEN = re.compile(
    r"(?i)\b(?:sk-(?:proj-|svcacct-)?[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}"
    r"|github_pat_[a-z0-9_]{16,}|Bearer\s+[^\s'\"<>]+)"
)
ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|password|passwd|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|gh[_-]?token|github[_-]?token)"
    r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


class Redactor:
    def __init__(self, environment=None):
        environment = os.environ if environment is None else environment
        self.secrets = sorted(
            {
                str(value)
                for key, value in environment.items()
                if re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|AUTHORIZATION|CREDENTIAL", key)
                and len(str(value)) >= 4
            },
            key=len,
            reverse=True,
        )

    def text(self, value, maximum=None):
        text = ANSI.sub("", str(value))
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = TOKEN.sub("[REDACTED]", text)
        text = ASSIGNMENT.sub(lambda match: match[1] + "[REDACTED]", text)
        text = "".join(c for c in text if c.isprintable() or c in "\n\t")
        # Consumer text must never become a GitHub workflow command.
        text = text.replace("::", ": :").replace("##[", "# #[")
        if maximum is not None and len(text.encode()) > maximum:
            text = text.encode()[: maximum - 20].decode(errors="ignore") + "\n[output truncated]"
        return text

    def data(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.data(item) for item in value]
        if isinstance(value, dict):
            return {key: self.data(item) for key, item in value.items()}
        return value


def markdown_text(value):
    value = html.escape(str(value)).replace("@", "&#64;")
    return re.sub(r"([\\*_\[\]])", r"\\\1", value).replace(chr(96), "\\" + chr(96))


def action_run(repository, run_key):
    if not REPO.fullmatch(repository or ""):
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,19}\.[1-9][0-9]{0,9}", str(run_key)):
        return None
    run, attempt = str(run_key).split(".")
    return f"https://github.com/{repository}/actions/runs/{run}/attempts/{attempt}"


def native_run_key():
    return f"{os.environ.get('GITHUB_RUN_ID', '')}.{os.environ.get('GITHUB_RUN_ATTEMPT', '')}"


def session_identity(context, role):
    from .adapters import adapter
    from .invocations import execution_config

    cfg = execution_config(context)
    settings = adapter(cfg["engine"]).session_settings(cfg, role)
    issue = context.get("issue", {})
    key = native_run_key()
    if not action_run(cfg["repository"], key):
        key = context.get("run_key", "")
    return {
        "schema": 1,
        "repository": cfg["repository"],
        "issue": issue.get("number") if isinstance(issue, dict) else issue,
        "pipeline": pipeline_id(cfg),
        "role": role,
        "invocation": context.get("invocation", {}).get("id", role),
        "context": digest(context),
        "reservation_run_key": context.get("run_key"),
        "run_key": key,
        "source": context.get("source"),
        "base": context.get("base"),
        "kit": cfg["kit"]["ref"],
        "model": settings.get("model"),
        "authentication": settings.get("authentication"),
    }


def regular_output(path):
    path = Path(path).absolute()
    require(not any(is_link(p) for p in (path, *path.parents)), "Linked diagnostic path")
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644
    )
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")


def diagnostic_json(path, value):
    """Public diagnostics created by root must remain readable by the uploader."""
    require(not is_link(Path(path)), "Linked diagnostic metadata")
    write_json(path, Redactor().data(value))
    Path(path).chmod(0o644)


def public_event(event, redactor):
    if not isinstance(event, dict) or event.get("kind") not in {
        "session",
        "command",
        "files",
        "message",
        "plan",
        "tool",
        "usage",
        "diagnostic",
        "heartbeat",
        "error",
    }:
        return None
    value = {"kind": event["kind"]}
    for key, maximum in (("status", 80), ("command", 2000), ("message", 6000), ("output", 16000)):
        if isinstance(event.get(key), str):
            value[key] = redactor.text(event[key], maximum)
    if type(event.get("exit_code")) is int:
        value["exit_code"] = event["exit_code"]
    if isinstance(event.get("files"), list):
        value["files"] = [
            redactor.text(path, 1000) for path in event["files"][:200] if isinstance(path, str)
        ]
    usage = event.get("usage")
    if isinstance(usage, dict):
        value["usage"] = {
            name: count
            for name, count in usage.items()
            if name
            in {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}
            and type(count) is int
            and 0 <= count <= 10**12
        }
    return value


class RunLog:
    def __init__(self, directory, identity, *, redactor=None, emit=None):
        self.directory = Path(directory).absolute()
        require(
            not any(is_link(p) for p in (self.directory, *self.directory.parents)),
            "Linked diagnostic directory",
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self.emit = (lambda line: print(line, flush=True)) if emit is None else emit
        self.started = time.monotonic()
        self.last_event = self.started
        self.bytes = 0
        self.events = 0
        self.omitted = 0
        self.commands = 0
        self.activity_bytes = 0
        self.state = {
            **identity,
            "started_at": now(),
            "status": "running",
            "diagnostics_only": True,
        }
        self.trace = regular_output(self.directory / "events.jsonl")
        self.activity = regular_output(self.directory / "activity.log")
        self.stderr = regular_output(self.directory / "stderr.log")
        self.record({"kind": "session", "status": "started", "message": "Agent process starting"})
        self.save()

    def save(self):
        diagnostic_json(
            self.directory / "run.json",
            {
                **self.state,
                "seconds": round(time.monotonic() - self.started, 2),
                "events": self.events,
                "commands": self.commands,
                "omitted_events": self.omitted,
            },
        )

    def record(self, event):
        safe = public_event(event, self.redactor)
        if safe is None:
            self.omitted += 1
            return
        safe["at"] = now()
        encoded = canonical(safe) + "\n"
        if safe["kind"] != "heartbeat":
            self.last_event = time.monotonic()
        if self.events >= MAX_EVENTS or self.bytes + len(encoded.encode()) > MAX_TRACE_BYTES:
            self.omitted += 1
            return
        self.trace.write(encoded)
        self.trace.flush()
        self.bytes += len(encoded.encode())
        self.events += 1
        if safe["kind"] == "command" and safe.get("status") == "started":
            self.commands += 1
        if "usage" in safe:
            self.state["usage"] = safe["usage"]
        label = safe.get("command") or safe.get("message") or ", ".join(safe.get("files", []))
        label = label or safe["kind"]
        lines = [
            f"{safe['kind']} {safe.get('status', '')}: {label}".rstrip(),
            *([safe["output"]] if safe.get("output") else []),
        ]
        for line in "\n".join(lines).splitlines():
            rendered = f"[{safe['at']}] {line}"
            size = len((rendered + "\n").encode())
            if self.activity_bytes + size > MAX_TRACE_BYTES:
                self.omitted += 1
                break
            self.activity.write(rendered + "\n")
            self.activity_bytes += size
            self.emit(rendered)
        self.activity.flush()
        self.save()

    def error_output(self, line):
        clean = self.redactor.text(line, 4000)
        if self.stderr.tell() + len((clean + "\n").encode()) <= MAX_TRACE_BYTES:
            self.stderr.write(clean + "\n")
            self.stderr.flush()
        self.record({"kind": "diagnostic", "message": clean})

    def heartbeat(self):
        self.record(
            {
                "kind": "heartbeat",
                "message": (
                    f"Agent process is running; elapsed {time.monotonic() - self.started:.0f}s; "
                    f"last CLI event {time.monotonic() - self.last_event:.0f}s ago"
                ),
            }
        )

    def finish(self, code, *, status=None):
        self.state.update(
            status=status or ("succeeded" if code == 0 else "failed"),
            exit_code=code,
            ended_at=now(),
        )
        self.record(
            {"kind": "session", "status": self.state["status"], "message": "Agent process ended"}
        )
        self.save()
        for handle in (self.trace, self.activity, self.stderr):
            handle.close()


def run_observed(
    argv,
    log,
    decode,
    *,
    timeout,
    stdin=None,
    env=None,
    cwd=None,
    private_log=None,
    heartbeat=HEARTBEAT_SECONDS,
    terminate=None,
    **identity,
):
    """Drain both pipes, stream selected events, and retain native exit/timeout behavior."""
    messages = queue.Queue(maxsize=16)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=(os.name != "nt"),
            **identity,
        )
    except (OSError, ValueError):
        log.finish(127, status="failed")
        raise
    lock = threading.Lock()
    readers_stopped = threading.Event()

    def send(kind, line):
        while not readers_stopped.is_set():
            try:
                messages.put((kind, line), timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def read(source, kind):
        pending, oversized = b"", False
        try:
            while chunk := source.read1(16384):
                if private_log:
                    with lock:
                        private_log.write(chunk)
                        private_log.flush()
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not send(kind, None if oversized or len(line) > MAX_LINE_BYTES else line):
                        return
                    oversized = False
                if len(pending) > MAX_LINE_BYTES:
                    pending, oversized = b"", True
            if pending or oversized:
                send(kind, None if oversized else pending)
        finally:
            send(kind, "eof")
            source.close()

    readers = [
        threading.Thread(target=read, args=(stream, kind), daemon=True)
        for stream, kind in ((proc.stdout, "stdout"), (proc.stderr, "stderr"))
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    next_heartbeat = time.monotonic() + heartbeat
    closed, status = set(), None
    exited_at = None

    def stop():
        if os.name == "nt":
            if proc.poll() is None:
                proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if terminate is None:
                    raise
                terminate(proc)

    try:
        while len(closed) < 2 or proc.poll() is None:
            clock = time.monotonic()
            if proc.poll() is not None:
                if exited_at is None:
                    exited_at = clock
                elif clock - exited_at >= 2:
                    stop()
                    log.record(
                        {
                            "kind": "diagnostic",
                            "message": "Output draining ended after process exit; remaining activity was omitted",
                        }
                    )
                    break
            if clock >= deadline and proc.poll() is None:
                status = "timed_out"
                stop()
            if clock >= next_heartbeat and proc.poll() is None:
                log.heartbeat()
                next_heartbeat = clock + heartbeat
            try:
                kind, line = messages.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None:
                    stop()  # Reap background descendants holding an inherited pipe open.
                continue
            if line == "eof":
                closed.add(kind)
            elif line is None:
                log.omitted += 1
            elif kind == "stderr":
                log.error_output(line.decode("utf-8", errors="replace"))
            else:
                try:
                    value = decode(json.loads(line))
                except (ValueError, TypeError, KeyError):
                    value = None
                if value is None:
                    log.omitted += 1
                else:
                    log.record(value)
        code = proc.wait(timeout=5)
        if status == "timed_out":
            code = 124
        log.finish(code, status=status)
        return code
    except BaseException:
        stop()
        proc.wait(timeout=5)
        log.finish(130, status="cancelled")
        raise
    finally:
        readers_stopped.set()
        for reader in readers:
            reader.join(timeout=1)
        for stream in (proc.stdout, proc.stderr):
            if not any(reader.is_alive() for reader in readers):
                stream.close()


def read_trace(path, redactor=None):
    redactor = redactor or Redactor()
    result = []
    for line in read_regular_bytes(path, MAX_TRACE_BYTES).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        safe = public_event(event, redactor)
        if safe:
            safe["at"] = redactor.text(event.get("at", ""), 80)
            result.append(safe)
        if len(result) >= MAX_EVENTS:
            break
    return result
