"""The engine's `sweep` call (docs/engine.md#the-sweep).

Each file in tests/conformance/sweeps/ is a case: the items that may start, the number that run,
the accounts at a usage limit, the policies `maxParallel` and `priority`, and the exact starts and
waits that the call must answer. The runner sends each case to `nexkit-engine sweep` on standard
input and to `POST /sweep`, and checks that both give the same text, which fits the answer's
schema.
"""

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from nexkit import engine, protocol
from nexkit.engine import cli
from tests.test_serve import ROOT, send, server

CASES = Path(__file__).resolve().parent / "conformance" / "sweeps"
EXAMPLES = ROOT / "nexkit" / "protocol" / "v1" / "examples" / "calls"


def cases():
    return [
        (path.stem, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(CASES.glob("*.json"))
    ]


def request_of(case):
    return {
        "protocol": 1,
        "call": "sweep",
        "policies": case["policies"],
        "running": case["running"],
        "limited": case["limited"],
        "items": case["items"],
    }


def on_standard_input(request):
    body = {key: value for key, value in request.items() if key != "call"}
    out = io.StringIO()
    code = cli.main(["sweep"], io.BytesIO(json.dumps(body).encode()), out)
    return code, out.getvalue()


class SweepCaseTests(unittest.TestCase):
    def test_each_case_gives_its_starts_and_waits_on_standard_input_and_over_http(self):
        self.assertGreaterEqual(len(cases()), 6)
        with server() as port:
            for name, case in cases():
                with self.subTest(name):
                    request = request_of(case)
                    self.assertEqual(protocol.check(request, "calls/sweep.schema.json"), [])
                    code, text = on_standard_input(request)
                    self.assertEqual(code, 0)
                    status, body = send(port, "/sweep", json.dumps(request).encode())
                    self.assertEqual((status, body.decode()), (200, text))
                    answer = json.loads(text)
                    self.assertEqual(protocol.check(answer, "calls/sweep.answer.schema.json"), [])
                    self.assertEqual(answer["starts"], case["starts"])
                    self.assertEqual(answer["waits"], case["waits"])
                    answered = [entry["item"] for entry in answer["starts"] + answer["waits"]]
                    self.assertCountEqual(answered, [item["item"] for item in case["items"]])

    def test_the_cases_together_start_beyond_the_limit_and_wait_for_both_reasons(self):
        found = {"beyondLimit": False, "place": False, "usageLimit": False}
        for _, case in cases():
            found["beyondLimit"] |= any(start["beyondLimit"] for start in case["starts"])
            for wait in case["waits"]:
                found[wait["for"]] = True
        self.assertEqual(found, {"beyondLimit": True, "place": True, "usageLimit": True})

    def test_the_same_request_gives_the_same_answer(self):
        for name, case in cases():
            with self.subTest(name):
                request = request_of(case)
                self.assertEqual(engine.answer(request), engine.answer(request))


class SweepCallTests(unittest.TestCase):
    def test_the_example_gets_the_example_answer(self):
        request = json.loads((EXAMPLES / "sweep.json").read_text(encoding="utf-8"))
        expected = json.loads((EXAMPLES / "sweep.answer.json").read_text(encoding="utf-8"))
        self.assertEqual(engine.answer(request), expected)

    def test_describe_lists_the_sweep(self):
        answer = engine.answer({"protocol": 1, "call": "describe"})
        self.assertEqual(answer["calls"], ["describe", "step", "prompt", "sweep"])

    def test_the_program_answers_as_a_process(self):
        request = json.loads((EXAMPLES / "sweep.json").read_text(encoding="utf-8"))
        text = json.dumps({key: value for key, value in request.items() if key != "call"})
        done = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "nexkit-engine"), "sweep"],
            input=text,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(done.stdout, engine.text(engine.answer(request)))

    def test_items_may_leave_out_everything_but_their_id(self):
        request = {"protocol": 1, "call": "sweep", "policies": {}, "running": 0,
                   "items": [{"item": "b"}, {"item": "a"}]}  # fmt: skip
        answer = engine.answer(request)
        self.assertEqual([s["item"] for s in answer["starts"]], ["b", "a"])
        self.assertEqual(answer["waits"], [])

    def test_a_request_that_breaks_the_call_is_refused_with_an_error_answer(self):
        base = {"protocol": 1, "call": "sweep", "policies": {}, "running": 0, "items": []}
        for what, request, words in (
            ("a duplicate", {**base, "items": [{"item": "1"}, {"item": "1"}]}, "names 1 twice"),
            ("no places", {**base, "policies": {"maxParallel": 0}}, "maxParallel"),
            ("a negative count", {**base, "running": -1}, "running"),
            ("no items", {key: v for key, v in base.items() if key != "items"}, "items"),
        ):
            with self.subTest(what):
                answer = engine.answer(request)
                self.assertEqual(protocol.check(answer, "calls/error.schema.json"), [], answer)
                self.assertIn(words, answer["error"])


if __name__ == "__main__":
    unittest.main()
