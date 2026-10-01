"""Markdown targets share the existing pinned adoption transaction."""
import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from skillopt_sleep.__main__ import _add_common, _cfg_from_args, main
from skillopt_sleep.config import load_config
from skillopt_sleep.cycle import run_sleep_cycle
from skillopt_sleep.staging import StagingError, adopt, write_staging
from skillopt_sleep.types import SleepReport


class DocumentTargets(unittest.TestCase):
    def stage(self, root, target, memory=None):
        return write_staging(
            str(root), report=SleepReport(night=1, project=str(root), accepted=True),
            proposed_skill="# Updated prompt\n", proposed_memory="# Local learning\n" if memory else None,
            live_skill_path=str(target), live_memory_path=str(memory or root / "CLAUDE.md"),
            report_md="# Report\n", document_targets=True,
        )

    def test_pipeline_and_project_guidance_adopt_with_backups(self):
        for name in ("commands/pipeline.md", "agents/builder.md", "AGENTS.md", "CLAUDE.local.md"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# Original\n")
                night = self.stage(root, target)
                self.assertEqual(target.read_text(), "# Original\n")
                self.assertEqual(adopt(night), [str(target)])
                self.assertEqual(target.read_text(), "# Updated prompt\n")
                self.assertEqual((Path(night) / "backup/SKILL.md").read_text(), "# Original\n")
                with self.assertRaises(StagingError):
                    adopt(night)

    def test_local_memory_adopts_independently_of_prompt_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target, memory = root / "pipeline.md", root / "CLAUDE.local.md"
            target.write_text("# Pipeline\n")
            memory.write_text("# Personal rules\n")
            night = self.stage(root, target, memory)
            self.assertEqual(adopt(night), [str(target), str(memory)])
            self.assertEqual(memory.read_text(), "# Local learning\n")
            self.assertEqual((Path(night) / "backup/CLAUDE.md").read_text(), "# Personal rules\n")

    def test_changed_live_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "AGENTS.md"
            target.write_text("# Original\n")
            night = self.stage(root, target)
            target.write_text("# Edited after review\n")
            with self.assertRaisesRegex(StagingError, "changed since staging"):
                adopt(night)
            self.assertEqual(target.read_text(), "# Edited after review\n")

    def test_proposal_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "pipeline.md"
            target.write_text("# Original\n")
            night = self.stage(root, target)
            (Path(night) / "proposed_SKILL.md").write_text("# Tampered\n")
            with self.assertRaises(StagingError):
                adopt(night)
            self.assertEqual(target.read_text(), "# Original\n")

    def test_non_markdown_and_colliding_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaisesRegex(StagingError, "unsafe|Markdown"):
                self.stage(root, root / "settings.json")
            target = root / "AGENTS.md"
            target.write_text("# Original\n")
            with self.assertRaisesRegex(StagingError, "same live file"):
                self.stage(root, target, target)

    def test_legacy_targets_keep_strict_basename_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaisesRegex(StagingError, "SKILL.md"):
                write_staging(str(root), report=SleepReport(night=1, project=tmp, accepted=True),
                    proposed_skill="# Prompt\n", proposed_memory=None,
                    live_skill_path=str(root / "pipeline.md"), live_memory_path=str(root / "CLAUDE.md"),
                    report_md="# Report\n")

    def test_manifest_target_collision_is_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target, memory = root / "pipeline.md", root / "CLAUDE.local.md"
            target.write_text("# Original\n")
            memory.write_text("# Personal\n")
            night = self.stage(root, target, memory)
            manifest_path = Path(night) / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            first, second = manifest["legacy"]["skill"], manifest["legacy"]["memory"]
            for key in ("live_path", "live_realpath", "live_sha256", "live_basename"):
                second[key] = first[key]
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(StagingError, "same live file"):
                adopt(night)
            self.assertEqual(target.read_text(), "# Original\n")
            self.assertEqual(memory.read_text(), "# Personal\n")

    def test_rollback_restores_both_markdown_targets(self):
        from skillopt_sleep import staging
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target, memory = root / "pipeline.md", root / "CLAUDE.local.md"
            target.write_text("# Original\n")
            memory.write_text("# Personal\n")
            night = self.stage(root, target, memory)
            original_replace = staging.os.replace
            failed = False
            def fail_memory(*args, **kwargs):
                nonlocal failed
                if len(args) > 1 and args[1] == str(memory) and not failed:
                    failed = True
                    raise OSError("injected replacement failure")
                return original_replace(*args, **kwargs)
            with mock.patch.object(staging.os, "replace", side_effect=fail_memory):
                with self.assertRaises(OSError):
                    adopt(night)
            self.assertEqual(target.read_text(), "# Original\n")
            self.assertEqual(memory.read_text(), "# Personal\n")

    def test_cli_paths_resolve_against_project(self):
        parser = argparse.ArgumentParser()
        _add_common(parser)
        with tempfile.TemporaryDirectory() as tmp, mock.patch("skillopt_sleep.config._user_config_path", return_value=None):
            args = parser.parse_args(["--project", tmp, "--target-document-path", "agents/builder.md",
                "--memory-path", "CLAUDE.local.md", "--no-memory"])
            cfg = _cfg_from_args(args)
            self.assertEqual(cfg.managed_skill_path(), os.path.join(tmp, "agents/builder.md"))
            self.assertEqual(cfg.managed_memory_path(), os.path.join(tmp, "CLAUDE.local.md"))
            self.assertFalse(cfg.evolve_memory)
            self.assertTrue(cfg.document_targets)

    def test_cycle_uses_configured_memory_and_preserves_prompt_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "AGENTS.md"
            target.write_text("# Existing instructions\nKeep original text.\n")
            local = root / "CLAUDE.local.md"
            local.write_text("# Personal guidance\n")
            cfg = load_config(**{"invoked_project": str(root), "projects": "invoked",
                "claude_home": str(root / ".claude"), "backend": "mock", "evidence_log": False,
                "target_skill_path": str(target), "memory_path": "CLAUDE.local.md", "document_targets": True})
            with mock.patch("skillopt_sleep.cycle.dream_consolidate") as consolidate:
                from skillopt_sleep.consolidate import ConsolidationResult
                from skillopt_sleep.types import TaskRecord
                # Capture exact baseline documents before returning a no-edit result.
                consolidate.return_value = ConsolidationResult(new_skill=target.read_text(), new_memory=local.read_text(),
                    accepted=False, gate_action="reject", baseline_score=0, candidate_score=0,
                    applied_edits=[], rejected_edits=[], holdout_baseline=[], holdout_candidate=[])
                run_sleep_cycle(cfg, seed_tasks=[TaskRecord(id="one", project=str(root), intent="use personal guidance")], dry_run=True)
            self.assertEqual(consolidate.call_args.args[2], target.read_text())
            self.assertEqual(consolidate.call_args.args[3], local.read_text())

    def test_document_runs_have_independent_harvest_cursors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = load_config(**{"invoked_project": str(root), "projects": "invoked",
                "claude_home": str(root / ".claude"), "backend": "mock", "evidence_log": False,
                "target_skill_path": str(root / "first.md"), "document_targets": True,
                "lookback_hours": 0})
            with mock.patch("skillopt_sleep.cycle.harvest_for_config", return_value=[]) as harvest:
                run_sleep_cycle(cfg)
                cfg.data["target_skill_path"] = str(root / "second.md")
                run_sleep_cycle(cfg)
                self.assertIsNone(harvest.call_args.kwargs["since_iso"])
                cfg.data["target_skill_path"] = str(root / "first.md")
                run_sleep_cycle(cfg)
                self.assertIsNotNone(harvest.call_args.kwargs["since_iso"])

    def test_task_metadata_preserves_document_target_mode(self):
        parser = argparse.ArgumentParser()
        _add_common(parser)
        with tempfile.TemporaryDirectory() as tmp, mock.patch("skillopt_sleep.config._user_config_path", return_value=None):
            cfg = _cfg_from_args(parser.parse_args(["--project", tmp]), task_meta={
                "target_document_path": "AGENTS.md", "memory_path": "CLAUDE.local.md", "evolve_memory": False})
            self.assertTrue(cfg.document_targets)
            self.assertEqual(cfg.managed_skill_path(), os.path.join(tmp, "AGENTS.md"))
            self.assertEqual(cfg.managed_memory_path(), os.path.join(tmp, "CLAUDE.local.md"))
            self.assertFalse(cfg.evolve_memory)

    def test_cli_discovery_finds_local_guidance_without_creating_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            local = root / "CLAUDE.local.md"
            local.write_text("# Personal guidance\n")
            output = io.StringIO()
            with redirect_stdout(output), mock.patch("skillopt_sleep.config._user_config_path", return_value=None):
                self.assertEqual(main(["discover", "--project", str(root), "--project-only", "--json",
                    "--match", "CLAUDE.local.md", "--claude-home", str(root / ".claude"),
                    "--codex-home", str(root / ".codex")]), 0)
            records = json.loads(output.getvalue())["documents"]
            self.assertEqual([r["path"] for r in records], [str(local)])
            self.assertTrue(records[0]["writable"])
            self.assertFalse((root / ".skillopt-sleep").exists())
            self.assertFalse((root / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
