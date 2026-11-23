# Host Guide: DeepSeek Harness (DSH)

Load this guide when DSH acts as the host agent driving `codex-use`.

## 1. Installing the Skill

Load via DSH bundle (`cordis.patch.yml`) or symlink `skills/codex-use` into `~/.agents/skills/codex-use`:

```bash
dsh --profile headless --patch /path/to/Codex-use/cordis.patch.yml --dump-config
```

The patch registers `@deepseek-ai/dsh-skill-filesystem` with `id: codex-use-skills`, `providerName: codex-use`, `includeDefaultRoots: false`, and `bundledSkillDir: ./skills`.

## 2. Host Permissions and Diff Review

Ensure the host DSH session can create sibling `.worktrees/` directories (for example via `--add-dir <parent-dir>` under `workspace-write`) and invoke target worker CLIs. Run `worktree create --preflight` or `preflight` before dispatching. Pair local `git diff <base>..HEAD` checks with `drive.py review --worker codex --cwd <wt> --base <base_sha>` when a second-model review pass is warranted.
