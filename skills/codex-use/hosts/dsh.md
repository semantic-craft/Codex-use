# Host Guide: DeepSeek Harness (DSH)

Read this guide when DeepSeek Harness itself acts as the host agent driving `codex-use` (for example, DSH driving `codex` for code reviews or delegating parallel worktree slices).

## 1. Loading the Skill in DSH

Install `codex-use` into a DSH profile as a bundle or load its `cordis.patch.yml` overlay directly:

```bash
dsh --profile headless --patch /path/to/Codex-use/cordis.patch.yml --dump-config
```

The patch registers `@deepseek-ai/dsh-skill-filesystem` with `id: codex-use-skills`, `providerName: codex-use`, `includeDefaultRoots: false`, and `bundledSkillDir: ./skills`.

Alternatively, symlink `skills/codex-use` into `~/.agents/skills/codex-use` so DSH discovers it through default skill roots.

## 2. Host Permission Scope When Driving Workers

Because the host DSH session runs `git worktree`, `drive.py`, and child worker CLIs on the local machine:

- Ensure the host DSH session has permission to create worktrees under the sibling `.worktrees/` directory (for example via `--add-dir <parent-dir>` under `workspace-write`) and to invoke the target worker CLI (`codex` or `dsh`).
- Run `python3 skills/codex-use/scripts/drive.py preflight --worker <worker> --cwd <repo>` before starting any slice to verify that sandbox boundaries, binary paths, and git metadata access are working.

## 3. Diff Audit and Review Pairing

DSH headless has no built-in review subcommand. When DSH is the host, pair local `git diff <base>..HEAD` checks with `drive.py review --worker codex --cwd <worktree> --base <base_sha>` whenever a second-model review pass is warranted.
