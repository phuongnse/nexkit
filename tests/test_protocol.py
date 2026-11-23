"""The engine's protocol: its schemas match docs/engine.md, and every example fits its schema."""

import json
import re
import unittest
from pathlib import Path

from nexkit import agent, protocol

DOC = Path(__file__).resolve().parent.parent / "docs" / "engine.md"
EXAMPLES = protocol.ROOT / "examples"


def table_names(heading):
    """The names in backticks in the first column of the table under `heading`."""
    text = DOC.read_text(encoding="utf-8").split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    return sorted({name for row in rows for name in re.findall(r"`(\w+)`", row.split("|")[1])})


class ProtocolTest(unittest.TestCase):
    def test_every_event_and_action_of_the_page_has_a_schema_and_no_other(self):
        self.assertEqual(protocol.names("events"), table_names("Events"))
        self.assertEqual(protocol.names("actions"), table_names("Actions"))

    def test_every_action_has_an_id_and_a_result(self):
        for kind in protocol.names("actions"):
            schema = protocol.load(f"actions/{kind}.schema.json")
            self.assertIn("id", schema["required"], kind)
            self.assertEqual(schema["properties"]["kind"], {"const": kind})
            result = protocol.ROOT / "actions" / f"{kind}.result.schema.json"
            self.assertTrue(result.exists(), kind)

    def test_a_result_event_names_its_action_and_the_change_version(self):
        for kind in ("actionFinished", "actionLost"):
            required = protocol.load(f"events/{kind}.schema.json")["required"]
            self.assertIn("action", required, kind)
            self.assertIn("changeVersion", required, kind)

    def test_the_state_document_has_a_schema_version_of_its_own(self):
        schema = protocol.load("state.schema.json")
        self.assertIn("schemaVersion", schema["required"])
        self.assertNotIn("protocol", schema["properties"])

    def test_a_sample_state_of_every_state_fits(self):
        states = protocol.load("common.schema.json")["$defs"]["state"]["enum"]
        samples = sorted(path.stem for path in (EXAMPLES / "state").glob("*.json"))
        self.assertEqual(samples, sorted(states))
        for name in states:
            value = json.loads((EXAMPLES / "state" / f"{name}.json").read_text())
            self.assertEqual(value["state"], name)
            self.assertEqual(protocol.check(value, "state.schema.json"), [], name)

    def test_every_example_fits_its_schema(self):
        examples = sorted(EXAMPLES.glob("*/*.json"))
        checked = set()
        for path in examples:
            folder = path.parent.name
            root = folder in ("state", "policies")
            name = f"{folder}.schema.json" if root else f"{folder}/{path.stem}.schema.json"
            value = json.loads(path.read_text())
            self.assertEqual(protocol.check(value, name), [], str(path))
            checked.add(name)
        schemas = {
            str(path.relative_to(protocol.ROOT))
            for path in protocol.ROOT.glob("*/*.schema.json")
            if path.parent.name != "examples"
        }
        schemas.discard("results/checklist.schema.json")
        self.assertEqual(schemas - checked, set())

    def test_a_value_that_breaks_a_schema_is_found(self):
        tick = {"protocol": 1, "kind": "tick", "item": 7, "at": "2026-11-23T15:01:11Z", "x": 1}
        self.assertEqual(
            protocol.check(tick, "events/tick.schema.json"),
            ["value.wait is missing", "value.item must be a string", "value.x is not allowed"],
        )

    def test_the_stages_read_their_result_schemas_from_the_protocol(self):
        for stage, schema in agent.SCHEMAS.items():
            self.assertEqual(schema, protocol.load(f"results/{stage}.schema.json"))
            self.assertNotIn("$comment", repr(schema))


if __name__ == "__main__":
    unittest.main()
