# Project and shared-tool learning

This fork extends SkillOpt-Sleep to discover instruction documents rather than
requiring a tool-specific list of paths. It learns textual procedures in protected
blocks. It does not edit project source code or train model weights.

1. Run `skillopt-sleep discover --project PATH --project-only --json` for project
   instructions. `CLAUDE.local.md`, `CLAUDE.md`, `AGENTS.md`, skills and native
   agent/command prompts are discovered when present.
2. For a shared tool, run discovery in its editable source repository. Discovery
   also follows local Markdown references to pipeline and stage prompts. Use
   `--match TEXT` to inspect a requested tool's installed records; generated
   installations are read-only and do not establish where editable source lives.
3. Select relevant records marked `writable: true`. Run one mock dry-run for each
   selected target using `--target-document-path PATH --no-memory --backend mock`.
   To include separate project memory, replace `--no-memory` with
   `--memory-path PATH`. Personal Claude rules can use discovered `CLAUDE.local.md`;
   Codex needs a target that its host consumes, such as `AGENTS.md` or a skill.
4. Project-specific evidence uses `--scope invoked`. Shared procedures may use
   `--scope all`. Choose `--source codex` or `--source claude` explicitly; `auto`
   selects one source and does not combine both hosts. Codex harvesting currently
   reads archived sessions, not active sessions.
5. A real `run` needs a selected backend and bounded `--max-sessions`/`--max-tasks`.
   Review/redact sensitive evidence or use a reviewed `--tasks-file`. No real
   optimization, scheduling, or automatic adoption occurs during discovery/install.
6. Review each staged report, exact proposed edits, validation evidence, and target
   paths. Adopt that exact night with `adopt --project PATH --staging NIGHT --legacy`.
   Actual tool/pipeline regression tests remain necessary before release.

Project lessons stay in that project's instruction files. Shared lessons belong
in the tool's source prompts and should be checked against several repositories.
Merge/release shared changes through the tool's normal distribution process;
editing generated caches produces changes that an update can replace.
