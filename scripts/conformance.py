#!/usr/bin/env python3
"""Write the engine's conformance suite as one file, and play such a file against an engine.

    conformance.py build OUT
    conformance.py check FILE (--url URL | --program COMMAND)

`build` writes every case of `tests/conformance/` and `tests/conformance/sweeps/` as one JSON
file (`nexkit/protocol/v1/conformance.schema.json`): each case by name, with its whole requests in
order and the exact text of the answer the engine gives. The cases in `imports/` stay out.

`check` sends each request of the file to an engine and prints each answer that differs. With
`--url` it sends `POST URL/<call>`. With `--program` it runs `COMMAND <call>` and writes the
request on its standard input. It exits 1 when an answer differs, 2 when the file does not fit
its schema.
"""

from __future__ import annotations

import argparse
import io
import json
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nexkit import __version__, protocol  # noqa: E402
from nexkit.engine import cli  # noqa: E402
from tests import test_conformance, test_sweep  # noqa: E402

SCHEMA = "conformance.schema.json"
URL_TIMEOUT = 30
PROGRAM_TIMEOUT = 60


def sweep_text(request):
    out = io.StringIO()
    cli.main(["sweep"], io.BytesIO(json.dumps(request).encode()), out)
    return out.getvalue()


def build(path):
    """Write the suite to `path`: the step cases first, then the sweep cases."""
    cases = []
    for case_path in test_conformance.cases():
        case = json.loads(case_path.read_text(encoding="utf-8"))
        requests = [
            {"call": "step", "request": request, "answer": text}
            for _, _, request, text in test_conformance.play(case)
        ]
        cases.append({"name": case_path.stem, "requests": requests})
    for stem, case in test_sweep.cases():
        request = test_sweep.request_of(case)
        requests = [{"call": "sweep", "request": request, "answer": sweep_text(request)}]
        cases.append({"name": f"sweeps/{stem}", "requests": requests})
    document = {
        "protocol": {"major": protocol.VERSION, "minor": protocol.MINOR},
        "nexkit": __version__,
        "cases": cases,
    }
    text = json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    Path(path).write_text(text, encoding="utf-8")
    return document


def over_http(url, call, body):
    """The status and text of the engine's answer, or a reason it gave none."""
    request = urllib.request.Request(
        f"{url.rstrip('/')}/{call}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=URL_TIMEOUT) as response:
            return response.status, response.read().decode("utf-8"), None
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace"), None
    except (OSError, ValueError) as error:
        return None, None, f"no answer: {error}"


def on_program(command, call, body):
    try:
        done = subprocess.run(
            [*shlex.split(command), call], input=body, capture_output=True, timeout=PROGRAM_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return None, None, f"no answer: {error}"
    return done.returncode, done.stdout.decode("utf-8", "replace"), None


def difference(entry, status, text, failed, statuses):
    """Why an engine's answer differs from the file's, or None. `statuses` is the status of an
    answer and of an error answer."""
    if failed:
        return failed
    if text != entry["answer"]:
        return "the answer differs"
    refused = "error" in json.loads(entry["answer"])
    expected = statuses[refused]
    if status != expected:
        return f"status {status}, expected {expected}"
    return None


def check(path, url=None, program=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = protocol.check(document, SCHEMA)
    if problems:
        print(f"{path} does not fit {SCHEMA}:", file=err)
        for problem in problems:
            print(f"  {problem}", file=err)
        return 2
    # The status of an answer: 200 or 0, and for an error 400 or 1.
    expected = (200, 400) if url else (0, 1)
    found, total = 0, 0
    for case in document["cases"]:
        for number, entry in enumerate(case["requests"], 1):
            total += 1
            body = json.dumps(entry["request"]).encode()
            if url:
                status, text, failed = over_http(url, entry["call"], body)
            else:
                status, text, failed = on_program(program, entry["call"], body)
            reason = difference(entry, status, text, failed, expected)
            if reason:
                found += 1
                print(f"{case['name']} request {number} ({entry['call']}): {reason}", file=out)
    if found:
        print(f"{found} of {total} answers differ", file=out)
        return 1
    print(f"{total} answers match", file=out)
    return 0


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(prog="conformance.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser("build", help="write the suite as one file")
    built.add_argument("out", metavar="OUT")
    checked = commands.add_parser("check", help="play a file against an engine")
    checked.add_argument("file", metavar="FILE")
    where = checked.add_mutually_exclusive_group(required=True)
    where.add_argument("--url", help="the engine's URL, as nexkit-engine serve prints it")
    where.add_argument("--program", help="a command that takes the call as its argument")
    args = parser.parse_args(argv)
    if args.command == "build":
        document = build(args.out)
        total = sum(len(case["requests"]) for case in document["cases"])
        print(f"{len(document['cases'])} cases, {total} requests", file=stdout or sys.stdout)
        return 0
    return check(args.file, args.url, args.program, stdout)


if __name__ == "__main__":
    sys.exit(main())
