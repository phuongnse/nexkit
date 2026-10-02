"""Codex configuration, CLI capability probes and native execution integration."""

import re
import subprocess
from pathlib import Path

from ..policy import VERSION, require

EXEC_OPTIONS = {
    "--config",
    "--model",
    "--cd",
    "--ignore-user-config",
    "--ignore-rules",
    "--strict-config",
    "--ephemeral",
    "--json",
    "--skip-git-repo-check",
    "--output-schema",
    "--output-last-message",
}


def command(binary, *args):
    prefix = binary if isinstance(binary, (list, tuple)) else [binary]
    return [*map(str, prefix), *args]


def version(binary, *, env=None, cwd=None):
    """Diagnostic metadata; an absent or unfamiliar release label is not a gate."""
    try:
        result = subprocess.run(
            command(binary, "--version"),
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip().removeprefix("codex-cli ") or None


class CodexAdapter:
    name = "codex"
    executable = "codex"
    control_directories = (".codex", ".agents")

    def authentication(self, engine):
        return engine.get("auth", "api-key")

    def validate(self, engine):
        require(set(engine) <= {"name", "auth", "install"}, "Unknown Codex adapter setting")
        require(
            isinstance(self.authentication(engine), str)
            and self.authentication(engine) in {"api-key", "chatgpt"},
            "Choose engine.auth: api-key or chatgpt",
        )
        install = engine.get("install", {})
        require(
            isinstance(install, dict) and set(install) <= {"version"},
            "engine.install accepts an optional CLI version pin",
        )
        if "version" in install:
            require(
                isinstance(install["version"], str) and VERSION.fullmatch(install["version"]),
                "Use an exact release for the optional installation pin",
            )

    def validate_runner(self, engine, runner):
        if self.authentication(engine) == "chatgpt":
            require(
                isinstance(runner, list),
                "ChatGPT CI authentication requires an explicitly configured self-hosted agent runner",
            )

    def validate_effort(self, effort, role):
        require(
            isinstance(effort, str)
            and effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"},
            f"Choose a supported reasoning_effort for {role}",
        )

    def session_settings(self, cfg, role):
        model_role = "review" if role == "review" else "implement"
        return {
            "adapter": self.name,
            "authentication": self.authentication(cfg["engine"]),
            "model": cfg["models"][model_role],
            "effort": cfg.get("reasoning_effort", {}).get(model_role, ""),
            "install_version": cfg["engine"].get("install", {}).get("version", ""),
            "sandbox": "workspace-write" if role == "deliver" else "read-only",
        }

    def probe(self, binary, *, env=None):
        result = subprocess.run(
            command(binary, "exec", "--help"),
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        options = set(re.findall(r"--[a-z][a-z-]*", result.stdout))
        missing = EXEC_OPTIONS - options
        require(
            not missing, f"Codex lacks required execution options: {', '.join(sorted(missing))}"
        )
        return {"adapter": self.name, "version": version(binary, env=env), "execution": True}

    def execute(self, context, role, *, data):
        from .codex_subscription import execute

        return execute(context, role, data=data)

    def prepare_observation(self, engine, *, data):
        if self.authentication(engine) == "api-key":
            from .codex_events import prepare_api_observer

            prepare_api_observer(data)

    def stop_session(self, engine):
        import pwd

        users = ["nexkit-agent"]
        if self.authentication(engine) == "chatgpt":
            users.append("nexkit-codex")
        for user in users:
            try:
                pwd.getpwnam(user)
            except KeyError:
                continue
            result = subprocess.run(["pkill", "-KILL", "-u", user], capture_output=True)
            require(result.returncode in (0, 1), "Could not stop the isolated agent account")

    def build_image(self, engine, toolkit_version, root):
        pin = engine.get("install", {}).get("version")
        image = f"nexkit-runner:{toolkit_version}-codex" + (f"-{pin}" if pin else "")
        command = ["docker", "build", "--tag", image]
        if pin:
            command += ["--build-arg", "CODEX_VERSION=" + pin]
        return image, [*command, str(Path(root) / "runner")]

    def login_command(self, image, root, security):
        return [
            "docker",
            "run",
            "--rm",
            "-it",
            "--network",
            "nexkit-runners",
            "--user",
            "1101:1101",
            "--env",
            "CODEX_HOME=/var/lib/nexkit/codex",
            *security,
            "--mount",
            f"type=bind,src={Path(root) / 'codex'},dst=/var/lib/nexkit/codex",
            image,
            "codex",
            "login",
            "--device-auth",
        ]

    def required_secret(self, engine):
        return "OPENAI_API_KEY" if self.authentication(engine) == "api-key" else None

    def home_configuration(self):
        return {".codex/config.toml": 'project_doc_max_bytes = 0\napproval_policy = "never"\n'}

    def prompt_constraints(self, engine):
        if self.authentication(engine) != "chatgpt":
            return ""
        return (
            "Runner constraints: tool network access, including local sockets, is disabled. "
            "Run applicable offline checks here. The separate verification job runs the "
            "declared network/HTTP E2E checks and supplies their actual results for review. "
            "Report unavailable checks honestly; do not change assertions or application "
            "behavior to bypass sandbox restrictions. Native CLI Git metadata is unavailable; "
            "use the sandboxed terminal's Git commands to inspect the supplied repository.\n"
        )
