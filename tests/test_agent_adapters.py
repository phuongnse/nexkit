"""Shared adapter contracts and capability failures; process boundaries are mocked."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nexkit.adapters import ADAPTERS, adapter, control_directories
from nexkit.adapters.codex import EXEC_OPTIONS, CodexAdapter
from nexkit.agent_session import execute
from nexkit.common import Blocked
from nexkit.policy import authentication, execution_settings, protected_path
from scripts.install_codex import install
from tests.support import project


class ExampleAdapter:
    """A different provider contract, without installing another CLI in CI."""

    name = "example"
    executable = "example-agent"
    control_directories = (".example",)

    def validate(self, engine):
        if engine != {"name": self.name}:
            raise Blocked("Unknown example adapter setting")

    def authentication(self, engine):
        return "example-login"

    def validate_runner(self, engine, runner):
        pass

    def validate_effort(self, effort, role):
        if effort != "focused":
            raise Blocked("Choose focused effort")

    def session_settings(self, cfg, role):
        return {"adapter": self.name, "authentication": self.authentication(cfg["engine"])}

    def probe(self, binary, *, env=None):
        return {"adapter": self.name, "execution": True}

    def execute(self, context, role, *, data):
        return {"adapter": self.name, "role": role}

    def prompt_constraints(self, engine):
        return "Example offline tool constraints"

    def build_image(self, engine, toolkit_version, root):
        return "example-runner", ["example-builder", str(root)]

    def login_command(self, image, root, security):
        return [self.executable, "login"]

    def required_secret(self, engine):
        return None

    def home_configuration(self):
        return {".example/settings.json": "{}\n"}


class AgentAdapterTests(unittest.TestCase):
    def test_registered_adapters_follow_the_shared_execution_contract(self):
        with patch.dict(ADAPTERS, {"example": ExampleAdapter()}):
            for name in ADAPTERS:
                with self.subTest(adapter=name):
                    cfg = project()
                    cfg["engine"] = {"name": name}
                    integration = adapter(cfg["engine"])
                    self.assertEqual(execution_settings(cfg), cfg)
                    self.assertEqual(authentication(cfg), integration.authentication(cfg["engine"]))
                    self.assertEqual(integration.session_settings(cfg, "review")["adapter"], name)
                    for directory in integration.control_directories:
                        self.assertIn(directory, control_directories())
                        self.assertTrue(protected_path(directory + "/config.toml"))
                    context = {"config": cfg}
                    with (
                        patch.object(
                            integration, "execute", return_value={"role": "review"}
                        ) as run,
                        patch(
                            "nexkit.agent_session.prepared",
                            return_value=(
                                cfg,
                                "linux",
                                Path("/home"),
                                Path("/home/work"),
                                Path("/home/output/result.json"),
                            ),
                        ),
                    ):
                        result = execute(context, "review", data="/prepared")
                    self.assertEqual(result, {"role": "review"})
                    run.assert_called_once_with(context, "review", data=Path("/prepared"))

    def test_provider_specific_effort_and_authentication_do_not_change_core(self):
        with patch.dict(ADAPTERS, {"example": ExampleAdapter()}):
            cfg = project()
            cfg["engine"] = {"name": "example"}
            cfg["reasoning_effort"] = {"implement": "focused", "review": "focused"}
            self.assertEqual(execution_settings(cfg), cfg)
            self.assertEqual(authentication(cfg), "example-login")

    def test_unknown_adapter_stops_before_execution(self):
        with self.assertRaisesRegex(Blocked, "adapter"):
            adapter({"name": "unregistered"})

    def test_installation_pin_is_optional_and_does_not_gate_runtime(self):
        integration = CodexAdapter()
        for engine in ({"name": "codex"}, {"name": "codex", "install": {"version": "1.2.3"}}):
            with self.subTest(engine=engine):
                cfg = project()
                cfg["engine"] = engine
                self.assertEqual(execution_settings(cfg), cfg)
                with patch(
                    "nexkit.adapters.codex.subprocess.run",
                    side_effect=[
                        SimpleNamespace(stdout="\n".join(EXEC_OPTIONS)),
                        SimpleNamespace(stdout="codex-cli development-build\n"),
                    ],
                ) as process:
                    self.assertTrue(integration.probe("codex")["execution"])
                self.assertEqual(process.call_args_list[0].args[0], ["codex", "exec", "--help"])
                self.assertEqual(process.call_args_list[1].args[0], ["codex", "--version"])

    def test_missing_diagnostic_version_does_not_block_execution(self):
        with patch(
            "nexkit.adapters.codex.subprocess.run",
            side_effect=[
                SimpleNamespace(stdout="\n".join(EXEC_OPTIONS)),
                subprocess.CalledProcessError(1, "codex"),
            ],
        ):
            self.assertEqual(
                CodexAdapter().probe("codex"),
                {"adapter": "codex", "version": None, "execution": True},
            )

    def test_missing_required_options_stops_before_login_or_model_execution(self):
        for missing in ("--ignore-user-config", "--strict-config", "--output-schema"):
            with (
                self.subTest(missing=missing),
                patch(
                    "nexkit.adapters.codex.subprocess.run",
                    return_value=SimpleNamespace(stdout="\n".join(EXEC_OPTIONS - {missing})),
                ) as process,
                self.assertRaisesRegex(Blocked, missing),
            ):
                CodexAdapter().probe("codex")
            self.assertEqual(process.call_count, 1)
            self.assertEqual(process.call_args.args[0], ["codex", "exec", "--help"])

    def test_installing_another_asset_requires_its_checksum_before_download(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("scripts.install_codex.urllib.request.urlopen", new=Mock()) as download,
        ):
            destination = Path(temporary) / "cli"
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                install(destination, version="1.2.3")
            download.assert_not_called()
            self.assertFalse(destination.exists())
