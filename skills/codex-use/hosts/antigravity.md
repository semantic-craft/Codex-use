# Host Guide: Antigravity

Load when Antigravity orchestrates `codex-use`.

- **Non-blocking execution:** invoke `drive.py` via `run_command` (`WaitMsBeforeAsync: 5000` for quick scout/`sessions`, `500` + `NotificationTimeoutSeconds` for background `run`/`resume`/`attach`). Prefer `run --daemon` (plus `--fast` for priority) to skip `exec`/MCP cold boot. Never poll or `sleep`.
- **Live TUI `attach`:** run `drive.py attach --cwd <repo> [--create] ...` asynchronously to pair with an open `codex` TUI or create a warm daemon thread. Default `attach` wakes on `approval_required`/`user_input_required` (`--wait-for-approval` waits).
- **Paths & review:** pass literal absolute paths (no `$VAR`/`$(...)`), keep briefs in `<worktree>/.codex-use/`, and review high-risk diffs in a blind `invoke_subagent` before routing `auto-fix` or `ask-user` per [`../SKILL.md`](../SKILL.md).
