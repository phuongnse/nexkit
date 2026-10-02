"""Agent integrations selected by contract, independently of CLI release numbers."""

from typing import Protocol

from ..common import Blocked
from .codex import CodexAdapter


class AgentAdapter(Protocol):
    name: str
    executable: str
    control_directories: tuple[str, ...]

    def validate(self, engine): ...
    def authentication(self, engine): ...
    def validate_runner(self, engine, runner): ...
    def validate_effort(self, effort, role): ...
    def session_settings(self, cfg, role): ...
    def probe(self, binary, *, env=None): ...
    def execute(self, context, role, *, data): ...
    def prompt_constraints(self, engine): ...
    def build_image(self, engine, toolkit_version, root): ...
    def login_command(self, image, root, security): ...
    def required_secret(self, engine): ...
    def home_configuration(self): ...


ADAPTERS: dict[str, AgentAdapter] = {"codex": CodexAdapter()}


def adapter(engine):
    if not isinstance(engine, dict) or not isinstance(engine.get("name"), str):
        raise Blocked("Choose an installed agent adapter in engine.name")
    try:
        return ADAPTERS[engine["name"]]
    except KeyError as exc:
        raise Blocked(f"No agent adapter is installed for {engine['name']}") from exc


def control_directories():
    return tuple(
        sorted({path for value in ADAPTERS.values() for path in value.control_directories})
    )
