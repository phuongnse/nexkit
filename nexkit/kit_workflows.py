"""Update literal reusable-job bindings without composing consumer workflows.

Only block mappings are edited. Native Actions remains the YAML validator;
ambiguous aliases, flow mappings and computed kit pins need deliberate setup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .policy import SHA, require

KEY = re.compile(r"^( *)([A-Za-z0-9_-]+|'[^']+'|\"[^\"]+\"):(?:[ \t]+(.*))?$")
SCALAR = re.compile(r"(?:'([^'\n]*)'|\"([^\"\\\n]*)\"|([^'\"#\n]+?))[ \t]*(?:#.*)?$")
LEVEL = {"none": 0, "read": 1, "write": 2}


@dataclass
class Field:
    line: int
    indent: int
    value: str


class Workflow:
    def __init__(self, content, name):
        self.lines = content.decode("utf-8").splitlines(keepends=True)
        self.name = name
        self.edits = {}

    def mapping(self, parent=None):
        value = parent.value.strip().split("#", 1)[0].strip() if parent else ""
        if parent and value == "{}":
            return {}
        require(
            parent is None or not value,
            f"{self.name}: use a block mapping for version selection",
        )
        start, outer = (parent.line + 1, parent.indent) if parent else (0, -1)
        result, indent = {}, None
        for number in range(start, len(self.lines)):
            line = self.lines[number].rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#") or line == "---":
                continue
            spaces = len(line) - len(line.lstrip(" "))
            if spaces <= outer:
                break
            if indent is None:
                indent = spaces
            if spaces > indent:
                continue
            require(spaces == indent, f"{self.name}: inconsistent mapping indentation")
            match = KEY.fullmatch(line)
            require(match is not None, f"{self.name}: unsupported mapping; reconcile setup first")
            key = match[2].strip("'\"")
            require(key not in result, f"{self.name}: duplicate YAML key {key}")
            result[key] = Field(number, spaces, match[3] or "")
        return result

    def literal(self, field):
        match = SCALAR.fullmatch(field.value.strip())
        require(match is not None, f"{self.name}: expected a literal workflow value")
        return next(x for x in match.groups() if x is not None).strip()

    def replace(self, field, before, after):
        require(
            self.literal(field) == before, f"{self.name}: kit binding differs from configuration"
        )
        line = self.lines[field.line]
        # Replace only this scalar. Preserve its quotes, comments and line ending.
        prefix = line.index(":") + 1
        self.edits[field.line] = line[:prefix] + line[prefix:].replace(before, after, 1)

    def permissions(self, field):
        if field is None:
            return {}
        value = field.value.strip().split("#", 1)[0].strip()
        if value in ("read-all", "write-all"):
            return {"*": value.removesuffix("-all")}
        values = {key: self.literal(item) for key, item in self.mapping(field).items()}
        require(
            all(value in LEVEL for value in values.values()), f"{self.name}: invalid permissions"
        )
        return values

    def grant(self, job, field, current, required):
        missing = {
            key: value
            for key, value in required.items()
            if LEVEL.get(current.get(key, current.get("*", "none")), -1) < LEVEL[value]
        }
        if not missing:
            return
        require(
            "*" not in current, f"{self.name}: reconcile permissions before selecting a release"
        )
        values = current | missing
        newline = "\r\n" if self.lines[job.line].endswith("\r\n") else "\n"
        indent = field.indent if field else next(iter(self.mapping(job).values())).indent
        block = " " * indent + "permissions:" + newline
        block += "".join(
            " " * (indent + 2) + f"{key}: {value}" + newline
            for key, value in sorted(values.items())
        )
        if field:
            self.edits[field.line] = block
            for item in self.mapping(field).values():
                self.edits[item.line] = ""
        else:
            self.edits[job.line] = self.lines[job.line] + block

    def render(self):
        return "".join(self.edits.get(i, line) for i, line in enumerate(self.lines)).encode("utf-8")

    def copied_binding(self, job, kit):
        """Recognize toolkit checkouts/actions outside reusable calls; ignore script text."""
        scalar_block = None
        for line in self.lines[job.line + 1 :]:
            text = line.rstrip("\r\n")
            if not text.strip() or text.lstrip().startswith("#"):
                continue
            indent = len(text) - len(text.lstrip(" "))
            if indent <= job.indent:
                break
            if scalar_block is not None:
                if indent > scalar_block:
                    continue
                scalar_block = None
            # Sequence entries can start the first key on the same line.
            sequence = re.match(r"^( *)- +", text)
            match = KEY.fullmatch(re.sub(r"^( *)- +", r"\1", text))
            if match is None:
                if kit["repository"] + "/" in text:
                    return True
                continue
            key, value = match[2].strip("'\""), match[3] or ""
            if value.lstrip().startswith(("[", "{", "&", "*")) and (
                kit["repository"] in value or kit["ref"] in value
            ):
                return True
            if re.match(r"[|>](?:[1-9][+-]?|[+-][1-9]?)?(?:\s|$)", value):
                scalar_block = indent + (len(sequence[0]) - len(sequence[1]) if sequence else 0)
                continue
            scalar = SCALAR.fullmatch(value.strip())
            if scalar is None:
                continue
            value = next(x for x in scalar.groups() if x is not None).strip()
            if (
                (key == "repository" and value == kit["repository"])
                or (key in {"ref", "kit_ref", "NEXKIT_REF"} and value == kit["ref"])
                or (key == "uses" and value.startswith(kit["repository"] + "/"))
            ):
                return True
        return False


def required_permissions(child, top):
    required = child.permissions(top.get("permissions"))
    require("*" not in required, f"{child.name}: unsupported reusable permission declaration")
    for job in child.mapping(top["jobs"]).values():
        properties = child.mapping(job)
        permissions = child.permissions(properties.get("permissions"))
        require("*" not in permissions, f"{child.name}: unsupported reusable permissions")
        for key, value in permissions.items():
            require(value in LEVEL, f"{child.name}: invalid permission {value}")
            if LEVEL[value] > LEVEL.get(required.get(key, "none"), 0):
                required[key] = value
    return required


def update_workflow(content, name, old, selected, kit):
    """Check called interfaces, then change only pins and required job permissions."""
    caller = Workflow(content, name)
    top = caller.mapping()
    require("jobs" in top, f"{name}: missing jobs mapping")
    inherited = caller.permissions(top.get("permissions"))
    references = 0
    prefix = old["repository"] + "/.github/workflows/"
    for job in caller.mapping(top["jobs"]).values():
        properties = caller.mapping(job)
        if "uses" not in properties:
            require(
                not caller.copied_binding(job, old),
                f"{name}: copied NexKit job needs setup reconciliation before selecting a release",
            )
            continue
        reference = caller.literal(properties["uses"])
        if not reference.startswith(old["repository"] + "/"):
            continue
        require(
            reference.startswith(prefix) and "@" in reference,
            f"{name}: unsupported NexKit reusable workflow reference",
        )
        workflow, ref = reference.removeprefix(prefix).split("@", 1)
        require(
            ref == old["ref"] and SHA.fullmatch(ref) and re.fullmatch(r"[\w-]+\.ya?ml", workflow),
            f"{name}: kit reference differs from accepted configuration",
        )
        path = kit / ".github/workflows" / workflow
        require(path.is_file(), f"NexKit {selected['version']} does not provide {workflow}")
        child = Workflow(path.read_bytes(), workflow)
        child_top = child.mapping()
        events = child.mapping(child_top["on"])
        require("workflow_call" in events, f"{workflow} is not a reusable workflow")
        contract = child.mapping(events["workflow_call"])
        inputs = child.mapping(contract["inputs"]) if "inputs" in contract else {}
        supplied = caller.mapping(properties["with"]) if "with" in properties else {}
        require(
            set(supplied) <= set(inputs),
            f"{name}: {workflow} does not support inputs {sorted(set(supplied) - set(inputs))}",
        )
        for key, item in inputs.items():
            definition = child.mapping(item)
            if "required" in definition and child.literal(definition["required"]) == "true":
                require(key in supplied, f"{name}: {workflow} requires input {key}")
        require(
            {"kit_repository", "kit_ref"} <= set(supplied),
            f"{name}: NexKit calls need literal kit_repository and kit_ref inputs",
        )
        caller.replace(properties["uses"], reference, prefix + workflow + "@" + selected["ref"])
        require(
            caller.literal(supplied["kit_repository"]) == old["repository"],
            f"{name}: kit repository input differs from configuration",
        )
        caller.replace(supplied["kit_ref"], old["ref"], selected["ref"])
        field = properties.get("permissions")
        current = caller.permissions(field) if field else inherited
        caller.grant(job, field, current, required_permissions(child, child_top))
        references += 1
    return caller.render(), references
