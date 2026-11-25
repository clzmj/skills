"""Claude role -> model policy, agent definitions, launch templates and installer."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import claude_models
import install_claude_agents
import question_answerers
from claude_models import ClaudeEffort, ClaudeModel, ClaudeRole

AGENTS = Path(__file__).resolve().parents[1] / "claude-agents"
AGENT_ROLES = {
    "rex-mechanical-worker.md": ClaudeRole.MECHANICAL,
    "rex-worker.md": ClaudeRole.NORMAL,
    "rex-deep-worker.md": ClaudeRole.HARD,
    "rex-reviewer.md": ClaudeRole.REVIEW,
}
READ_ONLY_TOOLS = {"Read", "Grep", "Glob"}
OWNED = "# managed-by: agent-tooling rex"


def frontmatter(path):
    lines = path.read_text().split("\n")
    assert lines[0] == "---", f"{path.name}: no frontmatter"
    end = lines.index("---", 1)
    # Flat "key: value" lines only; YAML lists or multi-line values would break this.
    return dict(line.split(": ", 1) for line in lines[1:end] if not line.startswith("#"))


class ResolverTests(unittest.TestCase):
    def test_policy_table(self):
        self.assertEqual(
            {role: claude_models.resolve(role) for role in ClaudeRole},
            {
                ClaudeRole.MECHANICAL: "haiku",
                ClaudeRole.NORMAL: "sonnet",
                ClaudeRole.HARD: "opus",
                ClaudeRole.REVIEW: "opus",
            },
        )

    def test_wire_value_resolves(self):
        self.assertEqual(claude_models.resolve("normal"), ClaudeModel.SONNET)

    def test_unknown_role_is_an_error(self):
        for role in ("", "default", "Normal", "opus", None):
            with self.subTest(role=role), self.assertRaises(ValueError):
                claude_models.resolve(role)  # ty: ignore[invalid-argument-type]
            with self.subTest(effort=role), self.assertRaises(ValueError):
                claude_models.effort(role)  # ty: ignore[invalid-argument-type]

    def test_effort_table(self):
        self.assertEqual(
            {role: claude_models.effort(role) for role in ClaudeRole},
            {
                ClaudeRole.MECHANICAL: ClaudeEffort.LOW,
                ClaudeRole.NORMAL: ClaudeEffort.MEDIUM,
                ClaudeRole.HARD: ClaudeEffort.HIGH,
                ClaudeRole.REVIEW: ClaudeEffort.HIGH,
            },
        )

    def test_bad_config_is_an_error(self):
        good = claude_models.CONFIG.read_text()
        broken = {
            "missing role": good.split("[roles.review]")[0],
            "extra role": good + '\n[roles.other]\nmodel = "opus"\n',
            "unknown model": good.replace('"haiku"', '"opus[1m]"'),
            "unknown effort": good.replace('"medium"', '"huge"'),
            "missing effort": good.replace('effort = "low"', ""),
            "unknown key": good + 'efort = "high"\n',
            "missing model": good.replace('model = "haiku"', ""),
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "claude_models.toml"
            for name, text in broken.items():
                path.write_text(text)
                with self.subTest(name), self.assertRaises((ValueError, KeyError)):
                    claude_models.load(path)


class AgentDefinitionTests(unittest.TestCase):
    def test_every_agent_file_is_mapped_to_a_role(self):
        self.assertEqual({path.name for path in AGENTS.glob("*.md")}, set(AGENT_ROLES))

    def test_every_agent_declares_its_role_model_and_effort(self):
        for name, role in AGENT_ROLES.items():
            with self.subTest(name):
                meta = frontmatter(AGENTS / name)
                self.assertEqual(meta["name"], Path(name).stem)
                self.assertTrue(meta["description"])
                self.assertEqual(meta.get("model"), claude_models.resolve(role))
                self.assertEqual(meta.get("effort"), claude_models.effort(role))

    def test_reviewer_has_only_read_only_tools(self):
        meta = frontmatter(AGENTS / "rex-reviewer.md")
        self.assertEqual(set(meta["tools"].split(", ")), READ_ONLY_TOOLS)
        self.assertNotIn("mcpServers", meta)


class LaunchTemplateTests(unittest.TestCase):
    def test_every_claude_answerer_launches_a_role_model(self):
        role_models = {claude_models.resolve(role) for role in ClaudeRole}
        configs = question_answerers.load_models(
            Path(__file__).with_name("question_models.toml")
        )
        claude = [c for c in configs.values() if c.model in question_answerers.CLAUDE_MAX_BUDGET_USD]
        self.assertTrue(claude)
        for config in claude:
            with self.subTest(config.model):
                argv = question_answerers.build_command(config)
                self.assertIn(argv[argv.index("--model") + 1], role_models)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.target = Path(temporary.name) / "agents"

    def run_installer(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            install_claude_agents.main(["--target", str(self.target), *extra])
        return json.loads(output.getvalue())

    def test_dry_run_writes_nothing(self):
        receipt = self.run_installer("--dry-run")
        self.assertEqual(len(receipt["changed"]), len(AGENT_ROLES))
        self.assertFalse(self.target.exists())

    def test_installs_then_is_idempotent(self):
        self.assertEqual(len(self.run_installer()["changed"]), len(AGENT_ROLES))
        for name in AGENT_ROLES:
            self.assertEqual((self.target / name).read_text(), (AGENTS / name).read_text())
        self.assertEqual(self.run_installer()["changed"], [])
        self.assertEqual(len(list(self.target.iterdir())), len(AGENT_ROLES))

    def backups(self):
        return list(self.target.glob("*.agent-tooling-backup-*"))

    def test_every_source_carries_the_ownership_marker(self):
        for name in AGENT_ROLES:
            with self.subTest(name):
                self.assertIn(OWNED, (AGENTS / name).read_text().split("\n"))

    def test_skips_an_unowned_file_and_leaves_others(self):
        self.target.mkdir()
        mine = self.target / "rex-worker.md"
        mine.write_text(f"mine, quoting {OWNED} inline\n")
        (self.target / "worker.md").write_text("keep\n")
        receipt = self.run_installer()
        self.assertEqual(receipt["skipped"], [str(mine.resolve())])
        self.assertEqual(len(receipt["changed"]), len(AGENT_ROLES) - 1)
        self.assertIn("mine", mine.read_text())
        self.assertEqual((self.target / "worker.md").read_text(), "keep\n")
        self.assertEqual(self.backups(), [])

    def test_force_replaces_an_unowned_file_with_a_backup(self):
        self.target.mkdir()
        (self.target / "rex-worker.md").write_text("mine\n")
        self.assertEqual(self.run_installer("--force")["skipped"], [])
        [backup] = self.backups()
        self.assertEqual(backup.read_text(), "mine\n")
        self.assertEqual((self.target / "rex-worker.md").read_text(), (AGENTS / "rex-worker.md").read_text())

    def test_replaces_an_owned_file_once(self):
        self.target.mkdir()
        old = f"---\n{OWNED}\nname: rex-worker\n---\nold\n"
        (self.target / "rex-worker.md").write_text(old)
        self.assertEqual(self.run_installer()["skipped"], [])
        self.assertEqual(self.run_installer()["changed"], [])
        [backup] = self.backups()
        self.assertEqual(backup.read_text(), old)
        self.assertEqual((self.target / "rex-worker.md").read_text(), (AGENTS / "rex-worker.md").read_text())

    def run_hooks(self, home, agent, script=Path(__file__).with_name("install_hooks.py")):
        return subprocess.run(
            [sys.executable, str(script), "--home", str(home), "--agents", agent],
            check=True, capture_output=True, text=True,
        )

    def test_hook_installer_installs_agents_only_for_claude_code(self):
        for agent, expected in (("codex", set()), ("claude-code", set(AGENT_ROLES))):
            with self.subTest(agent):
                home = self.target.parent / agent
                json.loads(self.run_hooks(home, agent).stdout)
                self.assertEqual({p.name for p in (home / ".claude/agents").glob("*.md")}, expected)

    def test_runtime_copy_keeps_installing_hooks_without_agent_sources(self):
        home = self.target.parent / "home"
        self.run_hooks(home, "claude-code")
        runtime = home / ".local/share/agent-tooling/rex/install_hooks.py"
        self.assertIn("skills repository", self.run_hooks(home, "claude-code", runtime).stderr)


if __name__ == "__main__":
    unittest.main()
