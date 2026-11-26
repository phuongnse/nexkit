"""The engine's protocol: its schemas match docs/engine.md, every example fits its schema, and
the stages' result schemas that agents receive stay NexKit 6's."""

import fnmatch
import json
import re
import tomllib
import unittest
from pathlib import Path

from nexkit import agent, protocol
from nexkit.schema import problems
from tests.support import make_config, profile_config
from tests.test_agent import CHECKLIST

HERE = Path(__file__).resolve().parent
# The pages that hold the engine's states, events and actions, and what a host decides.
PAGES = [HERE.parent / "docs" / name for name in ("engine-states.md", "engine-hosts.md")]
EXAMPLES = protocol.ROOT / "examples"
# What NexKit 6.12.2 gave each stage's agent as its result schema, as `json.dumps` wrote it,
# with no configuration and with profiles and a checklist.
GOLDEN = json.loads((HERE / "golden" / "agent-schemas.json").read_text())
# The keywords of a schema that an agent receives: `schema.problems` reads them without a
# reader of `$ref`s, and NexKit 6 sent no others.
AGENT_KEYWORDS = {"type", "enum", "properties", "required", "items", "description"}
# The schemas that stand for every action and every event.
UNIONS = {"actions": "action.schema.json", "events": "event.schema.json"}


def section(heading):
    for page in PAGES:
        parts = page.read_text(encoding="utf-8").split(f"\n## {heading}\n", 1)
        if len(parts) == 2:
            return parts[1].split("\n## ", 1)[0]
    raise AssertionError(f"no engine page has the section {heading}")


def table_names(heading, every_row=False):
    """The names in backticks in the first column of the table under `heading`: of the rows
    that start with one, or of `every_row`, such as "Plan approval (`planApproval`)"."""
    rows = [line for line in section(heading).splitlines() if line.startswith("| ")][2:]
    if not every_row:
        rows = [row for row in rows if row.startswith("| `")]
    return sorted({name for row in rows for name in re.findall(r"`(\w+)`", row.split("|")[1])})


def bullet_names(heading, start):
    """The names in backticks in the bullet under `heading` that starts with `start`."""
    text = section(heading).split(f"\n- **{start}", 1)[1].split("\n- ", 1)[0]
    return sorted(set(re.findall(r"`(\w+)`", text)))


def schema_files():
    return sorted(
        str(path.relative_to(protocol.ROOT))
        for path in protocol.ROOT.rglob("*.schema.json")
        if "examples" not in path.parts
    )


def objects(schema):
    """Every object schema in `schema`, nested ones included."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" or "properties" in schema:
            yield schema
        for value in schema.values():
            yield from objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from objects(value)


def example(name):
    return json.loads((EXAMPLES / name).read_text())


class ProtocolTest(unittest.TestCase):
    def test_every_event_and_action_of_the_page_has_a_schema_and_no_other(self):
        self.assertEqual(protocol.names("events"), table_names("Events"))
        self.assertEqual(protocol.names("actions"), table_names("Actions"))
        for folder, union in UNIONS.items():
            refs = [ref["$ref"] for ref in protocol.load(union)["anyOf"]]
            names = protocol.names(folder)
            self.assertEqual(refs, [f"{folder}/{name}.schema.json" for name in names])

    def test_every_action_has_an_id_and_a_result(self):
        for kind in protocol.names("actions"):
            schema = protocol.load(f"actions/{kind}.schema.json")
            self.assertIn("id", schema["required"], kind)
            self.assertEqual(schema["properties"]["kind"], {"const": kind})
            result = protocol.ROOT / "actions" / f"{kind}.result.schema.json"
            self.assertTrue(result.exists(), kind)

    def test_every_event_has_an_id(self):
        for kind in protocol.names("events"):
            schema = protocol.load(f"events/{kind}.schema.json")
            self.assertIn("id", schema["required"], kind)
            self.assertEqual(schema["properties"]["kind"], {"const": kind})

    def test_a_result_event_names_its_action_and_the_change_version(self):
        for kind in ("actionFinished", "actionLost"):
            required = protocol.load(f"events/{kind}.schema.json")["required"]
            self.assertIn("action", required, kind)
            self.assertIn("changeVersion", required, kind)

    def test_the_state_document_has_a_schema_version_of_its_own(self):
        schema = protocol.load("state.schema.json")
        self.assertIn("schemaVersion", schema["required"])
        self.assertNotIn("protocol", schema["properties"])

    def test_no_schema_or_example_has_a_usd_field(self):
        def walk(value, where):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertNotEqual(key, "usd", f"{where} has a usd key")
                    walk(item, f"{where}/{key}")
            elif isinstance(value, list):
                self.assertNotIn("usd", value, f"{where} names usd")
                for index, item in enumerate(value):
                    walk(item, f"{where}[{index}]")

        paths = sorted(protocol.ROOT.rglob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            walk(json.loads(path.read_text(encoding="utf-8")), str(path.relative_to(protocol.ROOT)))

    def test_a_state_document_with_used_usd_is_read_without_it(self):
        state = example("state/planning.json")
        old = {**state, "used": {**state["used"], "usd": 1.5}}
        self.assertIn("usd", " ".join(protocol.check(old, "state.schema.json")))
        self.assertEqual(protocol.upgrade(old), state)
        self.assertIn("usd", old["used"])  # the document given is left as it was
        for value in (state, None, [], "x", {"used": None}, {"used": []}):
            self.assertEqual(protocol.upgrade(value), value)

    def test_a_sample_state_of_every_state_fits(self):
        states = protocol.load("common.schema.json")["$defs"]["state"]["enum"]
        samples = sorted(path.stem for path in (EXAMPLES / "state").glob("*.json"))
        self.assertEqual(samples, sorted(states))
        for name in states:
            value = example(f"state/{name}.json")
            self.assertEqual(value["state"], name)
            self.assertEqual(protocol.check(value, "state.schema.json"), [], name)

    def test_the_state_keeps_each_pending_action_whole(self):
        state = example("state/fixing.json")
        self.assertEqual(protocol.check(state["pending"][0], "action.schema.json"), [])
        state["pending"] = [{"id": "a7.1", "kind": "runAgent"}]
        self.assertIn(
            "value.pending[0].stage is missing", protocol.check(state, "state.schema.json")
        )

    def test_every_example_fits_its_schema(self):
        checked = set()
        for path in sorted(EXAMPLES.glob("*/*.json")):
            folder = path.parent.name
            if folder in ("state", "policies"):
                name = f"{folder}.schema.json"
            else:
                name = f"{folder}/{path.stem}.schema.json"
            value = json.loads(path.read_text())
            self.assertEqual(protocol.check(value, name), [], str(path))
            checked.add(name)
            if folder in UNIONS and not path.stem.endswith(".result"):
                self.assertEqual(protocol.check(value, UNIONS[folder]), [], str(path))
                checked.add(UNIONS[folder])
        unchecked = set(schema_files()) - checked
        self.assertEqual(unchecked, {"common.schema.json", "results/checklist.schema.json"})

    def test_each_keyword_finds_a_value_that_breaks_it(self):
        tick = example("events/tick.json")
        new = example("state/new.json")
        cases = [
            # const, which a boolean never fits for a number, and a $ref to another file.
            (
                {**tick, "protocol": True, "item": 7},
                "events/tick.schema.json",
                ["value.protocol must be 1", "value.item must be a string"],
            ),
            # required.
            (
                {key: value for key, value in tick.items() if key != "at"},
                "events/tick.schema.json",
                ["value.at is missing"],
            ),
            # enum, and const again on a number.
            (
                {**new, "schemaVersion": True, "itemKind": "pull"},
                "state.schema.json",
                [
                    "value.schemaVersion must be 1",
                    "value.itemKind must be one of 'change', 'reviewOnly'",
                ],
            ),
            # anyOf, items and additionalProperties.
            (
                {**new, "change": "308", "parts": [7], "extra": 1},
                "state.schema.json",
                [
                    "value.change must be an object",
                    "value.parts[0] must be an object",
                    "value.extra is not allowed",
                ],
            ),
            # A list of types.
            (
                {**example("actions/runAgent.result.json"), "seconds": "4"},
                "actions/runAgent.result.schema.json",
                ["value.seconds must be an integer or a null"],
            ),
        ]
        for value, name, found in cases:
            with self.subTest(name=name, found=found):
                self.assertEqual(protocol.check(value, name), found)
        # minimum, maximum and maxItems.
        policies = {"fixRounds": -1, "conflictRounds": 11}
        self.assertEqual(
            protocol.check(policies, "policies.schema.json"),
            ["value.fixRounds must be at least 0", "value.conflictRounds must be at most 10"],
        )
        crowded = {**new, "recentEvents": [f"d-{n}" for n in range(21)]}
        self.assertEqual(
            protocol.check(crowded, "state.schema.json"),
            ["value.recentEvents must hold at most 20 items"],
        )
        # Past maxItems the items are not checked one by one; the state bounds its outbox.
        self.assertEqual(
            problems([{}, {}], {"maxItems": 1, "items": {"type": "string"}}),
            ["result must hold at most 1 items"],
        )
        outbox = {**new, "pending": [{}] * 65}
        self.assertEqual(
            protocol.check(outbox, "state.schema.json"),
            ["value.pending must hold at most 64 items"],
        )
        # check tells at most LIMIT problems, and stops looking soon after; without a limit,
        # as an agent's result is checked, every problem is told.
        many = {**new, "waits": [{}] * 60}
        found = protocol.check(many, "state.schema.json")
        self.assertEqual(len(found), protocol.LIMIT + 1)
        self.assertEqual(found[-1], "and more")
        self.assertEqual(found[0], "value.waits[0].id is missing")
        items = {"items": {"type": "string"}}
        self.assertEqual(len(problems([1] * 30, items)), 30)
        self.assertEqual(len(problems([1] * 30, items, limit=5)), 6)
        # An integer has no fraction written, deliberately (nexkit/schema.py).
        self.assertEqual(problems(1.0, {"type": "integer"}), ["result must be an integer"])
        self.assertEqual(problems(True, {"enum": [1, "1"]}), ["result must be one of 1, '1'"])
        self.assertEqual(problems(1, {"enum": [True]}), ["result must be one of True"])

    def test_the_keywords_beside_a_ref_apply_too(self):
        schema = {"$ref": "common.schema.json#/$defs/id", "enum": ["42"]}
        found = problems("7", schema, "value", protocol._refs, "state.schema.json")
        self.assertEqual(found, ["value must be one of '42'"])

    def test_a_ref_that_leaves_the_version_is_refused(self):
        with self.assertRaisesRegex(ValueError, r"\.\./\.\./x\.schema\.json leaves v1/"):
            problems(1, {"$ref": "../../x.schema.json"}, "value", protocol._refs, "a.json")

    def test_a_name_may_point_into_a_file(self):
        entry = {"number": 1, "answer": "maybe", "evidence": ""}
        self.assertEqual(
            protocol.check(entry, "results/checklist.schema.json#/$defs/review"),
            ["value.answer must be one of 'met', 'not_met', 'not_applicable'"],
        )

    def test_every_schema_names_its_dialect_and_uri(self):
        for name in schema_files():
            raw = json.loads((protocol.ROOT / name).read_text())
            self.assertEqual(raw["$schema"], protocol.DIALECT, name)
            self.assertEqual(raw["$id"], protocol.BASE + name, name)
            self.assertFalse(set(protocol.ANNOTATIONS) & set(protocol.load(name)), name)

    def test_each_addition_names_a_minor_version_that_exists(self):
        # docs/engine.md#versions: what 1.N adds carries "x-since": "1.N", and 1.0 adds nothing.
        def since(value):
            if isinstance(value, dict):
                if "x-since" in value:
                    yield value["x-since"]
                for sub in value.values():
                    yield from since(sub)
            elif isinstance(value, list):
                for sub in value:
                    yield from since(sub)

        named = set()
        for name in schema_files():
            raw = json.loads((protocol.ROOT / name).read_text())
            for value in since(raw):
                self.assertRegex(value, r"^1\.[1-9][0-9]*$", name)
                self.assertLessEqual(int(value.split(".")[1]), protocol.MINOR, name)
                named.add(int(value.split(".")[1]))
            self.assertNotIn("x-since", json.dumps(protocol.load(name)), name)
        self.assertEqual(named, set(range(1, protocol.MINOR + 1)))

    def test_load_leaves_out_the_annotations(self):
        # A made-up schema: what `load` gives an agent or a host holds no annotation.
        raw = {
            "$schema": protocol.DIALECT,
            "$id": protocol.BASE + "made-up.schema.json",
            "$comment": "For people.",
            "type": "object",
            "properties": {"new": {"x-since": "1.3", "$comment": "Added.", "type": "string"}},
        }
        self.assertEqual(
            protocol._plain(raw),
            {"type": "object", "properties": {"new": {"type": "string"}}},
        )

    def test_a_change_is_observed_whole_never_as_a_delta(self):
        shape = protocol.load("common.schema.json")["$defs"]["observation"]
        fields = shape["required"]
        # `behind` is the one field a host may leave out: it means the change is not behind.
        self.assertEqual(sorted(fields), sorted(set(shape["properties"]) - {"behind"}))
        self.assertNotIn("behind", fields)
        event = protocol.load("events/changeObserved.schema.json")
        state = protocol.load("state.schema.json")["properties"]["observed"]["anyOf"][0]
        for schema, base in ((event, "../common.schema.json"), (state, "common.schema.json")):
            # One shape: each field refers to the observation's own.
            self.assertLessEqual(set(fields), set(schema["required"]))
            for field in fields:
                ref = f"{base}#/$defs/observation/properties/{field}"
                self.assertEqual(schema["properties"][field], {"$ref": ref})
        for schema, base in ((event, "../common.schema.json"), (state, "common.schema.json")):
            ref = f"{base}#/$defs/observation/properties/behind"
            self.assertEqual(schema["properties"]["behind"], {"$ref": ref})
            self.assertNotIn("behind", schema["required"])
        for kind in ("publish", "replaceChange", "updateChange"):
            # The host reads the change when it handles the result of an action that changed it.
            self.assertIn(
                "observed", protocol.load(f"actions/{kind}.result.schema.json")["required"]
            )

    def test_describe_answers_the_major_and_minor_version(self):
        answer = example("calls/describe.answer.json")
        self.assertEqual(answer["protocol"]["major"], protocol.VERSION)
        self.assertEqual(answer["protocol"]["minor"], protocol.MINOR)
        schema = protocol.load("calls/describe.answer.schema.json")
        self.assertEqual(schema["properties"]["protocol"]["properties"]["major"]["const"], 1)

    def test_messages_take_fields_they_do_not_know_and_the_state_does_not(self):
        # docs/engine.md#versions: a new optional field keeps the protocol's version.
        for name in schema_files():
            if name == "state.schema.json":
                continue
            for schema in objects(protocol.load(name)):
                self.assertNotIn("additionalProperties", schema, name)
        for schema in objects(protocol.load("state.schema.json")):
            self.assertIs(schema.get("additionalProperties"), False, schema)
        tick = {**example("events/tick.json"), "priority": 3}
        self.assertEqual(protocol.check(tick, "events/tick.schema.json"), [])

    def test_the_state_keeps_only_the_policies_it_knows(self):
        policies = protocol.load("state.schema.json")["properties"]["policies"]
        names = protocol.load("policies.schema.json")["properties"]
        self.assertEqual(sorted(policies["properties"]), sorted(names))
        state = {**example("state/fixing.json")}
        state["policies"] = {**state["policies"], "start": "bySweep"}
        self.assertEqual(
            protocol.check(state, "state.schema.json"), ["value.policies.start is not allowed"]
        )

    def test_a_decision_names_what_it_decides_on(self):
        for kind, field in (
            ("planApproved", "plan"),
            ("answered", "asked"),
            ("requested", "plan"),
            ("changeApproved", "changeId"),
            ("changeApproved", "changeVersion"),
        ):
            self.assertIn(field, protocol.load(f"events/{kind}.schema.json")["required"], kind)

    def test_the_package_ships_every_schema_and_no_example(self):
        data = tomllib.loads((HERE.parent / "pyproject.toml").read_text())
        globs = data["tool"]["setuptools"]["package-data"]["nexkit"]
        package = HERE.parent / "nexkit"
        files = [str(path.relative_to(package)) for path in (package / "protocol").rglob("*")]
        shipped = {name for name in files if any(fnmatch.fnmatch(name, g) for g in globs)}
        self.assertEqual(shipped, {f"protocol/v1/{name}" for name in schema_files()})

    def test_the_page_lists_the_attention_reasons_and_the_waits(self):
        reasons = protocol.load("common.schema.json")["$defs"]["attentionReason"]["enum"]
        self.assertEqual(bullet_names("States", "Attention:"), sorted(reasons))
        waits = protocol.load("state.schema.json")["properties"]["waits"]["items"]
        self.assertEqual(
            bullet_names("States", "Wait:"), sorted(waits["properties"]["for"]["enum"])
        )

    def test_the_page_lists_the_kinds_of_a_request(self):
        rows = [
            line for line in section("Events").splitlines() if line.startswith("| `requested` |")
        ]
        self.assertEqual(len(rows), 1)
        kinds = sorted(set(re.findall(r"`(\w+)`", rows[0].split("|")[2])))
        what = protocol.load("events/requested.schema.json")["properties"]["what"]["enum"]
        self.assertEqual(kinds, sorted(what))

    def test_the_page_lists_the_kinds_of_an_accept_refusal(self):
        rows = [line for line in section("Actions").splitlines() if line.startswith("| `accept` |")]
        self.assertEqual(len(rows), 1)
        refused = rows[0].split("|")[3].split("refused with a kind:", 1)[1].split(";", 1)[0]
        kinds = sorted(set(re.findall(r"`(\w+)`", refused)))
        refusal = protocol.load("actions/accept.result.schema.json")["properties"]["refusal"]
        enum = next(option["enum"] for option in refusal["anyOf"] if "enum" in option)
        self.assertEqual(kinds, sorted(enum))

    def test_the_page_lists_what_a_show_shows(self):
        rows = [line for line in section("Actions").splitlines() if line.startswith("| `show` |")]
        self.assertEqual(len(rows), 1)
        kinds = sorted(set(re.findall(r"`(\w+)`", rows[0].split("|")[2])))
        what = protocol.load("actions/show.schema.json")["properties"]["what"]["enum"]
        self.assertEqual(kinds, sorted(what))

    def test_the_page_names_each_policy_and_its_default_fits(self):
        policies = protocol.load("policies.schema.json")["properties"]
        self.assertEqual(table_names("What a host decides", every_row=True), sorted(policies))
        for name, policy in policies.items():
            self.assertEqual(problems(policy["default"], policy), [], name)


class AgentSchemaTest(unittest.TestCase):
    """What agents receive stays NexKit 6's, though the schemas now live in the protocol."""

    def test_agents_receive_the_schemas_nexkit_6_gave(self):
        configs = {
            "plain": make_config(),
            "profilesAndChecklist": profile_config(review={"checklist": CHECKLIST}),
        }
        for name, cfg in configs.items():
            for stage in agent.SCHEMAS:
                with self.subTest(config=name, stage=stage):
                    given = json.dumps(agent.stage_schema(stage, cfg))
                    self.assertEqual(given, GOLDEN[name][stage])

    def test_the_stage_schemas_use_only_what_an_agent_check_reads(self):
        def walk(schema, where):
            self.assertLessEqual(set(schema), AGENT_KEYWORDS, where)
            if "type" in schema:
                self.assertIsInstance(schema["type"], str, where)
            for key, sub in schema.get("properties", {}).items():
                walk(sub, f"{where}.{key}")
            if "items" in schema:
                walk(schema["items"], f"{where}[]")

        for stage in agent.SCHEMAS:
            walk(protocol.load(f"results/{stage}.schema.json"), stage)
        for stage, entry in protocol.load("results/checklist.schema.json")["$defs"].items():
            walk(entry, f"checklist.{stage}")


if __name__ == "__main__":
    unittest.main()
