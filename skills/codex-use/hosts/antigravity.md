# Host Guide: Antigravity

Load when Antigravity orchestrates `codex-use`.

- **Non-blocking execution:** invoke `drive.py` via `run_command` (`WaitMsBeforeAsync: 5000` for quick scout/`sessions`, `500` with `NotificationTimeoutSeconds` for background `run`/`resume`/`attach`). Never poll or run `sleep`; rely on automatic wakeup. Give parallel `ship` slices separate worktrees.
- **Live TUI `attach`:** when a terminal `codex` TUI is open in `<repo>`, run `drive.py attach --cwd <repo> ...` asynchronously. Default `attach` returns immediately with `status="approval_required"` or `"user_input_required"` on terminal prompts so you can ask the user to approve instead of hanging (pass `--wait-for-approval` when waiting on terminal approval).
- **Literal paths and `.codex-use/`:** pass resolved literal absolute paths (no `$(...)`, backticks, or `$VAR`) for reliable approval prefix matching. Store briefs in `<worktree>/.codex-use/` (excluded via `<git_common_dir>/info/exclude`).
- **Review and escalation:** for high-risk diffs, spawn a fresh `invoke_subagent` with only goal, rubric, test output, and `git diff <base>..HEAD`; route findings via `auto-fix` (`drive.py resume`, max 2 rounds) or `ask-user` per [`../SKILL.md`](../SKILL.md). Silence is never consent. Rewrite user reports without raw JSONL.
