"""Hermetic, read-only instruction discovery checks; no providers or transcripts."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skillopt_sleep.config import SleepConfig

try:
    from skillopt_sleep.discovery import discover_documents
except ImportError:
    discover_documents = None


class TestInstructionDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.addCleanup(patch.stopall)
        patch("pathlib.Path.home", return_value=self.home).start()
        self.cfg = SleepConfig({
            "invoked_project": str(self.project),
            "claude_home": str(self.home / ".claude"),
            "codex_home": str(self.home / ".codex"),
            "skill_roots": [],
        })

    def write(self, relative, body="instructions\n"):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def documents(self):
        self.assertIsNotNone(discover_documents, "instruction discovery is missing")
        rows = discover_documents(self.cfg)
        json.dumps(rows)
        self.assertEqual(rows, discover_documents(self.cfg))
        return {row["path"]: row for row in rows}

    def test_guides_include_local_nested_and_untracked_files_without_prose_links(self):
        wanted = [self.write("project/" + name) for name in
                  ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "Codex.local.md", "src/AGENTS.md")]
        wanted[0].write_text("[ordinary document](notes.md)\n", encoding="utf-8")
        self.write("project/notes.md")
        for directory in ("node_modules", ".git", ".skillopt-sleep", ".cache", ".codex/cache"):
            self.write(f"project/{directory}/CLAUDE.md")
        rows = self.documents()
        self.assertEqual(set(rows), {str(path) for path in wanted})
        self.assertTrue(all(row["kind"] == "guidance" and row["writable"] for row in rows.values()))

    def test_guides_directly_inside_project_agent_config_roots_are_discovered(self):
        wanted = [self.write(f"project/{agent}/{name}")
                  for agent in (".claude", ".codex", ".agents", ".cursor", ".devin")
                  for name in ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "Codex.local.md")]
        for agent in (".claude", ".codex", ".agents", ".cursor", ".devin"):
            self.write(f"project/{agent}/cache/CLAUDE.md")
        rows = self.documents()
        self.assertEqual(set(rows), {str(path) for path in wanted})
        self.assertTrue(all(row["kind"] == "guidance" and row["writable"] for row in rows.values()))

    def test_symlinked_agent_config_guide_is_read_only(self):
        external = self.write("outside-guide.md", "[other](another.md)\n")
        self.write("another.md")
        config = self.project / ".claude"
        config.mkdir()
        guide = config / "CLAUDE.md"
        guide.symlink_to(external)
        rows = self.documents()
        self.assertEqual(set(rows), {str(guide)})
        self.assertFalse(rows[str(guide)]["writable"])

    def test_all_existing_skill_layouts_recursive_references_and_dedup(self):
        root = self.project / ".codex" / "skills"
        skill = self.write("project/.codex/skills/work/SKILL.md",
                           "[pipeline](references/pipeline.md) `references/pipeline.md`\n"
                           "@references/stage.md\n[network](https://example.com/secret.md)\n")
        pipeline = self.write("project/.codex/skills/work/references/pipeline.md", "`stage.md`\n")
        stage = self.write("project/.codex/skills/work/references/stage.md", "[cycle](pipeline.md)\n")
        native = [self.write(f"home/{directory}/skills/other/SKILL.md")
                  for directory in (".codex", ".agents", ".claude")]
        custom = self.write("source/skills/another/SKILL.md")
        self.cfg.data["skill_roots"] = [str(root), str(self.base / "source")]
        before = {str(path): path.read_bytes() for path in (skill, pipeline, stage, *native, custom)}
        rows = self.documents()
        self.assertEqual(set(rows), set(before))
        self.assertEqual(rows[str(pipeline)]["kind"], "prompt")
        self.assertEqual(rows[str(pipeline)]["discovered_from"], str(skill))
        self.assertTrue(all(row["writable"] for row in rows.values()))
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})

    def test_declared_package_runtime_and_installed_cache_are_read_only(self):
        runtime = self.base / "engine-runtime" / "v1" / "package"
        pipeline = self.write("engine-runtime/v1/package/references/pipeline.md", "`stage.md`\n")
        stage = self.write("engine-runtime/v1/package/references/stage.md")
        skill = self.write("home/.codex/skills/orchestrate/SKILL.md",
                           f'Package root: "{runtime}".\n[pipeline]({pipeline})\n'
                           "<!-- # arbitrary-owned: runtime-v1 -->\n")
        cache = self.write("home/.claude/plugins/cache/market/plugin/1.0/skills/task/SKILL.md")
        rows = self.documents()
        self.assertEqual(set(rows), {str(skill), str(pipeline), str(stage), str(cache)})
        self.assertTrue(all(not row["writable"] and row["reason"] for row in rows.values()))

    def test_external_reference_is_reported_but_never_recursively_followed(self):
        external = self.write("elsewhere/private.md", "`also-private.md`\n")
        self.write("elsewhere/also-private.md")
        skill = self.write("project/.agents/skills/test/SKILL.md", f"[outside]({external})\n")
        rows = self.documents()
        self.assertEqual(set(rows), {str(skill), str(external)})
        self.assertFalse(rows[str(external)]["writable"])
        self.assertIn("outside", rows[str(external)]["reason"])

    def test_symlink_and_hardlink_documents_cannot_be_writable(self):
        external = self.write("outside.md")
        hardlink = self.project / "AGENTS.md"
        os.link(external, hardlink)
        symlink = self.project / "CLAUDE.md"
        symlink.symlink_to(external)
        self.write("project/.agents/skills/test/SKILL.md", "[link](linked.md)\n")
        link = self.project / ".agents/skills/test/linked.md"
        link.symlink_to(external)
        rows = self.documents()
        self.assertFalse(rows[str(hardlink)]["writable"])
        self.assertFalse(rows[str(symlink)]["writable"])
        self.assertFalse(rows[str(link)]["writable"])

    def test_explicit_source_root_does_not_alias_same_named_installed_skill(self):
        installed = self.write("home/.codex/skills/same/SKILL.md", "<!-- # generic-owned: v1 -->\n")
        source = self.write("source/skills/same/SKILL.md", "[stage](../../references/stage.md)\n")
        prompt = self.write("source/references/stage.md")
        self.cfg.data["skill_roots"] = [str(self.base / "source")]
        rows = self.documents()
        self.assertFalse(rows[str(installed)]["writable"])
        self.assertTrue(rows[str(source)]["writable"])
        self.assertTrue(rows[str(prompt)]["writable"])

    def test_project_native_frontmatter_instruction_directories(self):
        agent = self.write("project/package/references/agents/reviewer.md",
                           "---\nname: reviewer\ndescription: review changes\n---\n`../commands/pipeline.md`\n")
        command = self.write("project/package/references/commands/pipeline.md",
                             "---\ndescription: pipeline\n---\nsteps\n")
        hidden = self.write("project/.claude/agents/build.md", "---\ntools: Read\n---\nbuild\n")
        skill = self.write("project/plugin/package/skills/local/SKILL.md")
        self.write("project/docs/agents/overview.md", "ordinary prose\n")
        self.write("project/.claude/projects/agents/session.md", "---\nname: private\n---\n")
        rows = self.documents()
        self.assertEqual(set(rows), {str(agent), str(command), str(hidden), str(skill)})
        self.assertTrue(all(rows[str(path)]["kind"] == "prompt" for path in (agent, command, hidden)))
        self.assertTrue(all(row["writable"] for row in rows.values()))

    def test_symlinked_explicit_root_does_not_authorize_writing_its_target(self):
        skill = self.write("elsewhere/skills/test/SKILL.md")
        alias = self.base / "alias"
        alias.symlink_to(skill.parent.parent, target_is_directory=True)
        self.cfg.data["skill_roots"] = [str(alias)]
        rows = self.documents()
        self.assertEqual(set(rows), {str(skill)})
        self.assertFalse(rows[str(skill)]["writable"])

    def test_prompt_metadata_must_be_in_frontmatter_and_bad_urls_do_not_crash(self):
        self.write("project/agents/overview.md", "---\ntitle: overview\n---\nname: ordinary prose\n")
        skill = self.write("project/.agents/skills/test/SKILL.md", "[bad](https://[broken/file.md)\n")
        rows = self.documents()
        self.assertEqual(set(rows), {str(skill)})

    def test_codex_installed_cache_uses_latest_existing_plugin_version(self):
        older = self.write("home/.codex/plugins/cache/market/plugin/1.9/skills/task/SKILL.md")
        newest = self.write("home/.codex/plugins/cache/market/plugin/1.10/skills/task/SKILL.md")
        rows = self.documents()
        self.assertEqual(set(rows), {str(newest)})
        self.assertNotIn(str(older), rows)
        self.assertFalse(rows[str(newest)]["writable"])


if __name__ == "__main__":
    unittest.main()
