"""Finds the writes outside the engine that no engine answer comes before, from the code.

A write is a dispatch, a merge, a branch update, a close, a cancel or a rerun on GitHub, an
autopilot step that starts, merges, closes or splits, or a route decision that closes a
pull request or an issue with what the engine answered. The engine answers through
`program.call`, which only `host.step` and the sweep call. A write is answered when a call that
reaches `program.call` runs before it on every path of its function. `tests/test_answers.py`
runs this on every module outside the engine, and `tests/test_autopilot.py` on autopilot's steps.
"""

import ast
from pathlib import Path

# What `.host` gives that is not an answer of the engine: a refusal and its words.
NOT_ANSWERS = ("Refused", "refused_reason")

# The kinds of autopilot step that start, merge, end or split an item's work.
STEPS = ("start", "merge", "close", "split")

# The calls on a GitHub client that start or end work.
WRITES = (
    "dispatch",
    "merge",
    "update_branch",
    "close_issue",
    "close_pull",
    "cancel_run",
    "rerun_failed_jobs",
)

# The decisions that a route job builds and a maintain job carries out.
TASKS = ("close_answer", "on_base")

# The call that is the engine's answer.
ROOT = "program.call"

# The writes that serve no item, or carry out an answer that an earlier job took from the engine.
# Each function is named whole: none of its writes is checked.
NAMED = {
    "autopilot.request": "the sweep's own dispatch serves no item",
    "report.after_merge": "the workflows on the base branch after a merge serve no item",
    "autopilot._check_run": "the default branch's CI reruns serve no item",
    "maintain.start_waiting": "the wait dispatch (WAIT_INPUTS) serves no item",
    "maintain.close_answered": "carries out route's close_answer decision, built after an answer",
    "maintain.on_base_round": "carries out route's on_base decision, built after an answer",
    "maintain.resume_paused": (
        "its step carries out a wait that the engine answered, and host.resumes asks again in "
        "the item's group"
    ),
}


def _calls(nodes):
    """The calls within the expressions `nodes`, as (name, place, node): `f` for `f(...)`,
    `a.f` for `a.f(...)` and `.f` for a call of an attribute of anything else."""
    found = []
    for top in nodes:
        for node in ast.walk(top):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                head = func.value.id if isinstance(func.value, ast.Name) else ""
                name = f"{head}.{func.attr}"
            else:
                continue
            found.append((name, (node.lineno, node.col_offset), node))
    return found


def _heads(stmt):
    """The expressions of `stmt` itself, not those of the statements within it: a condition,
    what a loop runs over, what a `with` opens, or a whole simple statement."""
    return [
        child
        for name, value in ast.iter_fields(stmt)
        for child in (value if isinstance(value, list) else [value])
        if isinstance(child, ast.AST) and not isinstance(child, (ast.stmt, ast.ExceptHandler))
    ]


def _leaves(block):
    return bool(block) and isinstance(block[-1], (ast.Return, ast.Raise))


def _passed(stmt, asks):
    """The calls that run before the statement after `stmt` does, whatever the branch taken:
    its own expressions, and the body of a `try` or `with`, which runs through. The branches
    of a loop do not count, since the loop may not run. Those of an `if` count only when
    every branch asks, that is has a call for which `asks` is true, or ends in `return` or
    `raise`: then whichever branch ran, the engine was asked or the function left. A branch
    that is missing counts as neither."""
    found = _calls(_heads(stmt))
    if isinstance(stmt, (ast.Try, ast.With)):
        for inner in stmt.body:
            found += _passed(inner, asks)
    elif isinstance(stmt, ast.If):
        branches = []
        for block in (stmt.body, stmt.orelse):
            calls = [call for inner in block for call in _passed(inner, asks)]
            branches.append((calls, any(asks(call) for call in calls) or _leaves(block)))
        if all(good for _, good in branches):
            found += [call for calls, _ in branches for call in calls]
    return found


def _before(stmts, target, asks):
    """The calls that run before `target`, which is within `stmts`, on the way to it, or None
    when it is not within them."""
    before = []
    for stmt in stmts:
        if not any(node is target for node in ast.walk(stmt)):
            before += _passed(stmt, asks)
            continue
        own = _calls(_heads(stmt))
        blocks = [getattr(stmt, "body", []), getattr(stmt, "orelse", [])]
        blocks.append(getattr(stmt, "finalbody", []))
        blocks += [handler.body for handler in getattr(stmt, "handlers", [])]
        for block in blocks:
            found = _before(block, target, asks)
            if found is not None:
                if isinstance(stmt, (ast.Try, ast.With)) and block is not stmt.body:
                    found = _passed_body(stmt, asks) + found
                return before + own + found
        return before + own
    return None


def _passed_body(stmt, asks):
    return [call for inner in stmt.body for call in _passed(inner, asks)]


def _own(function):
    """The nodes of `function` that are not within a function or class defined inside it."""
    found = []
    stack = list(function.body)
    while stack:
        node = stack.pop()
        found.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))
    return found


class Function:
    """One function of one module: its node, its name and the functions around it."""

    def __init__(self, module, qualname, node, outer):
        self.module = module
        self.qualname = qualname
        self.name = f"{module}.{qualname}"
        self.node = node
        self.outer = outer  # the functions it is defined in, nearest first
        self.nodes = _own(node)
        self.calls = [(n.func, n) for n in self.nodes if isinstance(n, ast.Call)]


class Index:
    """The functions of the modules in `sources`, with what each of their names stands for."""

    def __init__(self, sources):
        self.trees = {name: ast.parse(text) for name, text in sources.items()}
        self.functions = {}
        self.scopes = {}  # (module, qualname or None) -> its bindings
        for module, tree in self.trees.items():
            self._index(module, tree.body, "", [])
            self.scopes[(module, None)] = self._bindings(module, tree.body, [], "")

    def _index(self, module, body, prefix, outer):
        for stmt in _statements(body):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = prefix + stmt.name
                function = Function(module, qualname, stmt, list(outer))
                self.functions[function.name] = function
                self.scopes[(module, qualname)] = self._bindings(
                    module, stmt.body, [arg.arg for arg in _arguments(stmt)], qualname + "."
                )
                self._index(module, stmt.body, qualname + ".", [function, *outer])
            elif isinstance(stmt, ast.ClassDef):
                self._index(module, stmt.body, prefix + stmt.name + ".", outer)

    def _package(self, module):
        """The parts of the package that relative imports of `module` start from."""
        parts = module.split(".")
        if any(other.startswith(module + ".") for other in self.trees):
            return parts
        return parts[:-1]

    def _bindings(self, module, body, arguments, prefix):
        """What each name bound directly in `body` stands for: ("func", dotted name) for a
        function defined or imported, ("mod", name) for a module imported, or None for a name
        that is a variable or an argument."""
        found = {name: None for name in arguments}
        package = self._package(module)
        for stmt in _statements(body):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[stmt.name] = ("func", f"{module}.{prefix}{stmt.name}")
            elif isinstance(stmt, ast.ImportFrom) and stmt.level:
                base = package[: len(package) - (stmt.level - 1)]
                if stmt.module:
                    base = base + stmt.module.split(".")
                for alias in stmt.names:
                    full = ".".join([*base, alias.name])
                    as_module = stmt.module is None or full in self.trees
                    found[alias.asname or alias.name] = ("mod" if as_module else "func", full)
            else:
                for node in _stored(stmt):
                    found.setdefault(node, None)
        return found

    def resolve(self, function, name):
        """What `name` stands for in `function`: the nearest scope that binds it wins."""
        scopes = [function, *function.outer]
        for scope in scopes:
            bound = self.scopes[(scope.module, scope.qualname)]
            if name in bound:
                return bound[name]
        return self.scopes[(function.module, None)].get(name)

    def target(self, function, func):
        """The dotted name of the function that a call of `func` calls, or None."""
        if isinstance(func, ast.Name):
            bound = self.resolve(function, func.id)
            return bound[1] if bound and bound[0] == "func" else None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            bound = self.resolve(function, func.value.id)
            if bound and bound[0] == "mod":
                return f"{bound[1]}.{func.attr}"
        return None

    def is_module(self, function, node):
        if not isinstance(node, ast.Name):
            return False
        bound = self.resolve(function, node.id)
        return bool(bound and bound[0] == "mod")


def _statements(body):
    """The statements of `body`, and those within its `if`, `try`, `with` and loops, but not
    within a function or a class: what a scope binds."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            yield from _statements(getattr(stmt, field, []))
        for handler in getattr(stmt, "handlers", []):
            yield from _statements(handler.body)


def _stored(stmt):
    return [
        node.id
        for top in _heads(stmt)
        for node in ast.walk(top)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    ]


def _arguments(function):
    args = function.args
    found = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return found + [arg for arg in (args.vararg, args.kwarg) if arg]


def real_sources(root=None):
    """The text of every module of `nexkit/` that this checks, by module name: all but the
    engine and `github.py`, which defines the writes."""
    root = Path(root or Path(__file__).resolve().parent.parent / "nexkit")
    found = {}
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[0] == "engine" or parts == ("github",):
            continue
        if parts[-1] == "__init__" and len(parts) > 1:
            parts = parts[:-1]
        found[".".join(parts)] = path.read_text(encoding="utf-8")
    return found


def _task_decision(node):
    """Whether `node` is a dict literal whose "task" is one of `TASKS`."""
    return isinstance(node, ast.Dict) and any(
        isinstance(key, ast.Constant)
        and key.value == "task"
        and isinstance(value, ast.Constant)
        and value.value in TASKS
        for key, value in zip(node.keys, node.values, strict=True)
    )


def unanswered_writes(sources, named=NAMED):
    """The functions of `sources` (module name to text) that hold a write with no engine call
    before it on some path, and that nothing calls: the top of each chain. A helper that writes
    with no call of the engine before it passes the write to its callers, which are judged
    instead, whether they asked first. A function that builds a decision of `TASKS` with no
    call of the engine before it is reported itself. The functions in `named` are not checked.
    Only direct calls by name, and by the name of a module imported, are followed."""
    index = Index(sources)
    targets = {}  # id of a call -> the dotted name it calls
    for function in index.functions.values():
        for func, call in function.calls:
            targets[id(call)] = index.target(function, func)

    asking = set()
    grown = True
    while grown:
        grown = False
        for function in index.functions.values():
            if function.name in asking:
                continue
            reached = (targets[id(call)] for _, call in function.calls)
            if any(target == ROOT or target in asking for target in reached):
                asking.add(function.name)
                grown = True

    def asks(call):
        target = targets.get(id(call[2]))
        return target == ROOT or target in asking

    def answered(function, node):
        ways = _before(function.node.body, node, asks) or []
        return any(asks(call) and call[1] < _place(node) for call in ways)

    writes = {}  # a function's name -> its writes: (node, is_decision)
    for function in index.functions.values():
        if function.name in named:
            continue
        found = []
        for func, call in function.calls:
            if _is_write(index, function, func, call):
                found.append(call)
        found += [node for node in function.nodes if _task_decision(node)]
        writes[function.name] = found
    unanswered = {
        name: [node for node in nodes if not answered(index.functions[name], node)]
        for name, nodes in writes.items()
    }

    tainted = {
        name
        for name, nodes in unanswered.items()
        if any(not isinstance(node, ast.Dict) for node in nodes)
    }
    grown = True
    while grown:
        grown = False
        for name, function in index.functions.items():
            if name in tainted or name in named:
                continue
            for _, call in function.calls:
                if targets[id(call)] in tainted and not answered(function, call):
                    tainted.add(name)
                    grown = True
                    break

    called = {
        targets[id(call)]
        for function in index.functions.values()
        for _, call in function.calls
        if targets[id(call)] != function.name
    }
    found = {name for name in tainted if name not in called}
    found |= {
        name for name, nodes in unanswered.items() if any(isinstance(n, ast.Dict) for n in nodes)
    }
    return sorted(found)


def _place(node):
    return (node.lineno, node.col_offset)


def _is_write(index, function, func, call):
    """Whether `call` is a call on GitHub that starts or ends work, or an autopilot step of one
    of `STEPS`."""
    if isinstance(func, ast.Attribute) and func.attr in WRITES:
        return not index.is_module(function, func.value)
    name = ""
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name == "_step" and len(call.args) >= 2:
        kind = call.args[1]
        return isinstance(kind, ast.Constant) and kind.value in STEPS
    return False
