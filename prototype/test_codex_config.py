"""Regression checks for actual TOML types and literal filesystem key names."""
import os
from pathlib import Path
import runpy
import tomllib
import unittest
from unittest.mock import patch

APP = runpy.run_path(str(Path(__file__).with_name("orchestrator.py")), run_name="codex_config_regression")


import fixture_runs as TF

class CodexConfigTests(unittest.TestCase):
    def test_environment_is_toml_table(self):
        environment = {"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows",
                       "PATH": r"C:\Python312;C:\Windows\System32;C:\Program Files\Git\cmd"}
        argument = APP["overrides"]({"shell_environment_policy.set": environment})[1]
        self.assertEqual(tomllib.loads(argument)["shell_environment_policy"]["set"], environment)

    def test_filesystem_rules_have_literal_keys_and_unchanged_access(self):
        run = APP["BASE"] / "runs/synthetic_config_regression"
        for name, writable in (("toydev", True), ("toytest", False)):
            args = APP["overrides"](APP["permissions"](run, name, writable))
            data = tomllib.loads("\n".join(args[1::2]))
            profile = data["permissions"][name]
            filesystem = profile["filesystem"]
            self.assertEqual(data["windows"]["sandbox"], "mxc")
            self.assertIs(profile["network"]["enabled"], False)
            self.assertEqual(filesystem[":minimal"], "read")
            self.assertEqual(filesystem[str(Path.home())], "deny")
            self.assertEqual(filesystem[str(run / "repo")], "write" if writable else "read")
            for path in (".git", "test_toy_math.py"):
                self.assertEqual(filesystem[str(run / "repo" / path)], "read")
            for path in (".claude", ".codex", ".mcp.json", "AGENTS.md", "AGENTS.override.md", "CLAUDE.md", ".agents"):
                self.assertEqual(filesystem[str(run / "repo" / path)], "deny")

    def test_developer_argv_has_typed_config_without_legacy_sandbox_override(self):
        run = TF.toy_run()
        argv = APP["commands"](run)["codex"]
        position = argv.index("exec")
        config = tomllib.loads("\n".join(argv[2:position:2]))
        self.assertEqual(config["default_permissions"], "toydev")
        self.assertEqual(config["approval_policy"], "never")
        self.assertEqual(config["forced_login_method"], "chatgpt")
        self.assertEqual(config["shell_environment_policy"]["inherit"], "none")
        self.assertFalse(config["features"]["hooks"])
        self.assertFalse(config["features"]["plugins"])
        self.assertNotIn("--sandbox", argv)
        self.assertIsInstance(config["shell_environment_policy"]["set"], dict)

    def test_agents_md_discovery_disabled_while_protected_paths_stay_denied(self):
        # Regression: a sandboxed AGENTS.md probe hit the deny rule -> os error 5 at thread/start.
        run = TF.toy_run()
        for diagnostic in (False, True):
            argv = APP["commands"](run, exec_diagnostic=diagnostic)["codex"]
            config = tomllib.loads("\n".join(argv[2:argv.index("exec"):2]))
            self.assertEqual(config["project_doc_max_bytes"], 0)
            filesystem = config["permissions"]["toydev"]["filesystem"]
            for path in (".claude", ".codex", ".mcp.json", "AGENTS.md", "AGENTS.override.md", "CLAUDE.md", ".agents"):
                self.assertEqual(filesystem[str(run / "repo" / path)], "deny")


    def test_unfrozen_environment_keeps_nonempty_system_drive(self):
        # Regression: without verified_startup_environment.json, MXC refused to start
        # ("MXC requires nonempty Unicode host SystemDrive").
        run = APP["BASE"] / "runs/synthetic_config_regression"
        self.assertFalse((run / "artifacts/verified_startup_environment.json").exists())
        self.assertEqual(APP["env"](run)["SystemDrive"], os.environ["SystemDrive"])
        with patch.dict(os.environ):
            del os.environ["SystemDrive"]
            self.assertEqual(APP["env"](run)["SystemDrive"], Path(os.environ["SystemRoot"]).drive)


if __name__ == "__main__":
    unittest.main()
