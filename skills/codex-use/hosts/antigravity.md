# Host Guide: Antigravity

Load when Antigravity orchestrates `codex-use`.

- **Non-blocking execution:** invoke `drive.py` via `run_command` (`WaitMsBeforeAsync: 5000` for quick scout, `500` for parallel background runs). Never poll or run `sleep`; wait for automatic wakeup. Give parallel `ship` slices separate worktrees with zero file overlap.
- **Literal-path approvals and `.codex-use/`:** invoke `drive.py` with resolved literal absolute paths (no `$(...)`, backticks, or `$VAR`) so command-approval prefix matching hits reliably. Store local briefs under `<worktree>/.codex-use/` (auto-excluded via `<git_common_dir>/info/exclude`).
- **Blind-context review:** for high-risk or cross-module diffs, spawn a fresh `invoke_subagent` reviewer with only goal, rubric, verification output, and `git diff <base>..HEAD` (no implementation chat history). Route findings through `auto-fix` (`drive.py resume`, max 2 rounds) or `ask-user` per [`../SKILL.md`](../SKILL.md).
- **Escalation and reporting:** escalate `ask-user` directly or via `ask_question` with options, impact, and recommendation; silence or timeout is never consent or permission to skip. Rewrite final user reports cleanly without raw JSONL or internal flags.
