"""The engine's protocol: its schemas match docs/engine.md, every example fits its schema, and
the stages' result schemas that agents receive stay NexKit 6's."""

import contextlib
import copy
import fnmatch
import importlib.util
import io
import json
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from nexkit import agent, engine, host, protocol
from nexkit.schema import problems
from tests.support import make_config, profile_config
from tests.test_agent import CHECKLIST

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "protocol_freeze", HERE.parent / "scripts" / "protocol_freeze.py"
)
freeze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freeze)
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


def first_cells(text):
    """The first cell of each row of the first table in `text`, after its header and separator."""
    cells, started = [], False
    for line in text.splitlines():
        if line.startswith("| "):
            started = True
            cells.append(line.split("|")[1].strip())
        elif started:
            break
    return cells[2:]


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
            if folder in ("state", "policies", "conformance"):
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
        # `behind` and `acceptable` are the fields a host may leave out: each means the change is
        # not, as far as the host can tell.
        optional = {"behind", "acceptable"}
        self.assertEqual(sorted(fields), sorted(set(shape["properties"]) - optional))
        self.assertFalse(optional & set(fields))
        event = protocol.load("events/changeObserved.schema.json")
        state = protocol.load("state.schema.json")["properties"]["observed"]["anyOf"][0]
        for schema, base in ((event, "../common.schema.json"), (state, "common.schema.json")):
            # One shape: each field refers to the observation's own.
            self.assertLessEqual(set(fields), set(schema["required"]))
            for field in fields:
                ref = f"{base}#/$defs/observation/properties/{field}"
                self.assertEqual(schema["properties"][field], {"$ref": ref})
        for schema, base in ((event, "../common.schema.json"), (state, "common.schema.json")):
            for field in sorted(optional):
                ref = f"{base}#/$defs/observation/properties/{field}"
                self.assertEqual(schema["properties"][field], {"$ref": ref})
                self.assertNotIn(field, schema["required"])
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

    def test_the_page_names_what_the_engine_shows(self):
        # The row names the values the engine asks for, and marks each other value of the enum
        # as one the engine never asks for.
        rows = [line for line in section("Actions").splitlines() if line.startswith("| `show` |")]
        self.assertEqual(len(rows), 1)
        asked_text, never_text = rows[0].split("|")[2].split("The protocol also allows", 1)
        asked = set(re.findall(r"`(\w+)`", asked_text))
        never = set(re.findall(r"`(\w+)`", never_text))
        source = (HERE.parent / "nexkit" / "engine" / "machine.py").read_text(encoding="utf-8")
        self.assertEqual(asked, set(re.findall(r'do\(\s*"show",\s*what="(\w+)"', source)))
        what = set(protocol.load("actions/show.schema.json")["properties"]["what"]["enum"])
        self.assertEqual(never, what - asked)
        self.assertEqual(asked | never, what)

    def test_the_page_names_each_policy_and_its_default_fits(self):
        policies = protocol.load("policies.schema.json")["properties"]
        self.assertEqual(table_names("What a host decides", every_row=True), sorted(policies))
        for name, policy in policies.items():
            self.assertEqual(problems(policy["default"], policy), [], name)

    def test_the_github_host_sets_each_policy(self):
        # The GitHub table under "Hosts" names each policy in the same first cell as the table
        # under "What a host decides", and the names in those cells are the policies the GitHub
        # host gives the engine. The test above ties that table to policies.schema.json.
        sentence = "The GitHub host sets each policy and its own settings as follows."
        parts = section("Hosts").split(sentence, 1)
        self.assertEqual(len(parts), 2, "the sentence above the GitHub table changed")
        github = first_cells(parts[1])
        decided = first_cells(section("What a host decides"))
        rows = [cell for cell in github if cell in decided]
        self.assertEqual(sorted(rows), sorted(decided))
        names = sorted({name for cell in rows for name in re.findall(r"`(\w+)`", cell)})
        self.assertEqual(names, sorted(host.parent_policies(make_config())))


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


def mutated(base, name, change):
    """A copy of the schemas `base` in which `change` has changed the file `name`."""
    new = copy.deepcopy(base)
    change(new[name])
    return new


def add_file(base, name, schema):
    new = copy.deepcopy(base)
    new[name] = schema
    return new


class CompatibilityTest(unittest.TestCase):
    """The rules of docs/engine.md#versions, proven on copies made here: the copy of a release
    in `tests/golden/protocol/` exists only from 7.0's release pull request."""

    tick = "events/tick.schema.json"
    state = "state.schema.json"

    def setUp(self):
        self.base = freeze.read(protocol.ROOT)

    def refused(self, new, expected, minor=0):
        found = freeze.problems(self.base, new, minor)
        self.assertTrue(any(expected in line for line in found), f"{expected!r} not in {found!r}")

    def test_the_tree_matches_a_copy_of_itself(self):
        self.assertEqual(freeze.problems(self.base, copy.deepcopy(self.base), 0), [])

    def test_each_refused_change_is_found(self):
        def rename(schema):
            schema["properties"]["waitId"] = schema["properties"].pop("wait")
            schema["required"][schema["required"].index("wait")] = "waitId"

        def seconds(schema):
            schema["properties"]["seconds"]["type"] = "string"

        cases = {
            "removed property": (
                mutated(self.base, self.tick, lambda s: s["properties"].pop("wait")),
                f"{self.tick}#/properties/wait was removed",
            ),
            "renamed property": (
                mutated(self.base, self.tick, rename),
                f"{self.tick}#/properties/wait was removed",
            ),
            "renamed property, new name": (
                mutated(self.base, self.tick, rename),
                f"{self.tick}#/properties/waitId was added",
            ),
            "new required property": (
                mutated(
                    self.base,
                    self.tick,
                    lambda s: (
                        s["properties"].update(extra={"type": "integer", "x-since": "1.1"}),
                        s["required"].append("extra"),
                    ),
                ),
                f"{self.tick}#/properties/extra was added",
            ),
            "required list": (
                mutated(self.base, self.tick, lambda s: s["required"].pop()),
                f"{self.tick}#/required changed",
            ),
            "changed type": (
                mutated(self.base, "actions/runAgent.result.schema.json", seconds),
                "actions/runAgent.result.schema.json#/properties/seconds/type changed",
            ),
            "new enum value": (
                mutated(
                    self.base,
                    "events/requested.schema.json",
                    lambda s: s["properties"]["what"]["enum"].append("poke"),
                ),
                "events/requested.schema.json#/properties/what/enum changed",
            ),
            "changed const": (
                mutated(
                    self.base,
                    self.tick,
                    lambda s: s["properties"]["kind"].update(const="tock"),
                ),
                f"{self.tick}#/properties/kind/const changed",
            ),
            "narrower limit": (
                mutated(
                    self.base,
                    "policies.schema.json",
                    lambda s: s["properties"]["conflictRounds"].update(maximum=5),
                ),
                "policies.schema.json#/properties/conflictRounds/maximum changed",
            ),
            "new event kind": (
                add_file(
                    mutated(
                        self.base,
                        "event.schema.json",
                        lambda s: s["anyOf"].append({"$ref": "events/poked.schema.json"}),
                    ),
                    "events/poked.schema.json",
                    {"type": "object"},
                ),
                "events/poked.schema.json was added",
            ),
            "new event kind in the union": (
                mutated(
                    self.base,
                    "event.schema.json",
                    lambda s: s["anyOf"].append({"$ref": "events/poked.schema.json"}),
                ),
                "event.schema.json#/anyOf changed",
            ),
            "new action kind": (
                add_file(self.base, "actions/poke.schema.json", {"type": "object"}),
                "actions/poke.schema.json was added",
            ),
            "removed file": (
                {name: s for name, s in self.base.items() if name != self.tick},
                f"{self.tick} was removed",
            ),
            "changed description": (
                mutated(
                    self.base,
                    self.tick,
                    lambda s: s["properties"]["wait"].update(description="Another."),
                ),
                f"{self.tick}#/properties/wait/description was added",
            ),
        }
        for name, (new, expected) in cases.items():
            with self.subTest(name):
                self.refused(new, expected)

    def test_a_new_optional_property_with_a_later_x_since_is_allowed(self):
        def add(since):
            prop = {"type": "integer"}
            if since:
                prop["x-since"] = since
            return mutated(self.base, self.tick, lambda s: s["properties"].update(extra=prop))

        self.assertEqual(freeze.problems(self.base, add("1.1"), 0), [])
        self.assertEqual(freeze.problems(self.base, add("1.3"), 2), [])
        for name, since, minor in (
            ("no x-since", None, 0),
            ("x-since not above", "1.0", 0),
            ("x-since of the copy", "1.1", 1),
            ("x-since of another major", "2.5", 0),
            ("x-since that is not a version", "soon", 0),
        ):
            with self.subTest(name):
                self.refused(add(since), f"{self.tick}#/properties/extra was added", minor)

    def test_a_new_optional_property_deep_in_a_schema_is_allowed(self):
        name = "policies.schema.json"
        new = mutated(
            self.base,
            name,
            lambda s: s["properties"].update(extra={"type": "integer", "x-since": "1.1"}),
        )
        self.assertEqual(freeze.problems(self.base, new, 0), [])

    def test_a_changed_comment_is_allowed(self):
        def comment(schema):
            schema["$comment"] = "Another text."
            schema["properties"]["wait"]["$comment"] = "A new one."

        self.assertEqual(freeze.problems(self.base, mutated(self.base, self.tick, comment), 0), [])

    def test_the_state_schema_changes_only_with_its_schema_version(self):
        def limit(schema):
            schema["properties"]["recentEvents"]["maxItems"] += 1

        def raised(schema):
            limit(schema)
            schema["properties"]["schemaVersion"]["const"] += 1

        def optional(schema):
            schema["properties"]["extra"] = {"type": "integer", "x-since": "1.1"}

        current = self.base[self.state]["properties"]["schemaVersion"]["const"]
        self.assertIn("recentEvents", self.base[self.state]["properties"])
        self.refused(mutated(self.base, self.state, limit), f"{self.state}#/properties")
        self.refused(
            mutated(self.base, self.state, optional), f"{self.state}#/properties/extra was added"
        )
        self.assertEqual(freeze.problems(self.base, mutated(self.base, self.state, raised), 0), [])
        lowered = mutated(
            self.base,
            self.state,
            lambda s: s["properties"]["schemaVersion"].update(const=current - 1),
        )
        self.refused(lowered, f"{self.state}#/properties/schemaVersion/const changed")

    def test_the_script_copies_every_schema_and_compares_with_its_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            freeze.write(tmp)
            found = {
                path.relative_to(tmp).as_posix() for path in Path(tmp).rglob("*") if path.is_file()
            }
            self.assertEqual(found, {*schema_files(), "version.json"})
            version = json.loads((Path(tmp) / "version.json").read_text())
            self.assertEqual(version, {"major": protocol.VERSION, "minor": protocol.MINOR})
            self.assertEqual(
                freeze.problems(freeze.read(tmp), freeze.read(protocol.ROOT), protocol.MINOR), []
            )
            for name in schema_files():
                self.assertEqual(
                    (Path(tmp) / name).read_bytes(), (protocol.ROOT / name).read_bytes(), name
                )
            mutant = mutated(freeze.read(tmp), self.tick, lambda s: s["properties"].pop("wait"))
            self.assertNotEqual(freeze.problems(freeze.read(tmp), mutant, protocol.MINOR), [])

    def test_the_script_refuses_a_tree_that_breaks_its_copy_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            freeze.write(tmp)
            path = Path(tmp) / self.tick
            schema = json.loads(path.read_text())
            schema["properties"]["extra"] = {"type": "integer"}
            path.write_text(json.dumps(schema))
            before = {p: p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = freeze.main(["--out", tmp])
            self.assertEqual(code, 1)
            self.assertIn("extra was removed", out.getvalue())
            after = {p: p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}
            self.assertEqual(after, before)

    def test_the_script_replaces_a_copy_that_the_tree_keeps(self):
        with tempfile.TemporaryDirectory() as tmp:
            freeze.write(tmp)
            stale = Path(tmp) / "events" / "old.schema.json"
            (Path(tmp) / "version.json").write_text('{"major": 0, "minor": 9}\n')
            stale.write_text("{}")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(freeze.main(["--out", tmp]), 0)
            self.assertFalse(stale.exists())
            self.assertEqual(
                json.loads((Path(tmp) / "version.json").read_text()),
                {"major": protocol.VERSION, "minor": protocol.MINOR},
            )

    def test_the_tree_keeps_the_copy_of_the_last_release(self):
        # `tests/golden/protocol/` is empty until 7.0's release pull request runs
        # `scripts/protocol_freeze.py` (docs/engine.md#versions): the rules are proven above.
        golden = HERE / "golden" / "protocol"
        if not (golden / "version.json").exists():
            return
        version = json.loads((golden / "version.json").read_text())
        self.assertEqual(version["major"], protocol.VERSION)
        self.assertLessEqual(version["minor"], protocol.MINOR)
        self.assertEqual(
            freeze.problems(freeze.read(golden), freeze.read(protocol.ROOT), version["minor"]), []
        )


class StateDocumentsTest(unittest.TestCase):
    """The engine reads each state document that a host stored at an older schema version."""

    folder = HERE / "golden" / "state"

    def current(self):
        return protocol.load("state.schema.json")["properties"]["schemaVersion"]["const"]

    def test_there_is_a_folder_for_each_schema_version(self):
        names = sorted(path.name for path in self.folder.iterdir())
        self.assertEqual(names, [f"v{n}" for n in range(1, self.current() + 1)])

    def test_the_engine_steps_each_document_and_answers_the_current_version(self):
        policies = example("calls/step.json")["policies"]
        for path in sorted(self.folder.glob("v*/*.json")):
            doc = json.loads(path.read_text())
            name = f"{path.parent.name}/{path.name}"
            with self.subTest(name):
                waits = doc.get("waits") or []
                event = {
                    "protocol": 1,
                    "kind": "tick",
                    "id": f"d-golden-{path.parent.name}-{path.stem}",
                    "item": doc["item"],
                    "at": "2026-11-23T15:01:11Z",
                    "wait": waits[0]["id"] if waits else "w0.0",
                }
                request = {
                    "protocol": 1,
                    "call": "step",
                    "state": doc,
                    "event": event,
                    "policies": policies,
                }
                answer = engine.answer(request)
                self.assertNotIn("error", answer, answer.get("error"))
                self.assertEqual(protocol.check(answer["state"], "state.schema.json"), [])
                self.assertEqual(answer["state"]["schemaVersion"], self.current())

    def test_each_folder_holds_a_document_of_each_state(self):
        for folder in self.folder.iterdir():
            states = {json.loads(path.read_text())["state"] for path in folder.glob("*.json")}
            every = set(protocol.load("common.schema.json")["$defs"]["state"]["enum"])
            self.assertEqual(states, every, folder.name)


if __name__ == "__main__":
    unittest.main()
