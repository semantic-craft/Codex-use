# Host Guide: Antigravity

Read this guide when Antigravity is the host orchestrating `codex-use`.

## 1. Non-Blocking Worker Execution

Launch `scripts/drive.py run`, `resume`, or `review` with `run_command` and let long-running invocations move to the background (`WaitMsBeforeAsync: 5000` for quick scout checks, `500` when intentionally launching parallel worktree runs).

- Do not poll in a loop or run shell `sleep` commands while waiting for `drive.py`. Antigravity wakes the host automatically when a background command completes.
- For parallel `ship` slices, create separate worktrees via `drive.py worktree create` first, verify zero file overlap, and launch one background command per worktree.

## 2. Literal-Path Command Approvals and `.codex-use/` Specs

Invoke `scripts/drive.py` with resolved literal absolute paths instead of shell expansions (`$(...)`, backticks, or `$VAR`). Command-approval prefix matching hits reliably when the command string starts with literal paths and stable flags.

Store local task prompts and rework specs under `.codex-use/` inside the repository or worktree (for example, `/path/to/worktree/.codex-use/task.md`). `scripts/drive.py` registers `.codex-use/` idempotently in `<git_common_dir>/info/exclude` whenever `worktree create` runs or `--prompt-file` points inside `<git_root>/.codex-use/`, keeping specs untracked without touching `.gitignore`.

## 3. Independent Blind-Context Review

For high-risk, security-sensitive, or cross-module diffs, spawn a fresh review subagent via `invoke_subagent` before merging:

- Pass only the task objective, review rubric, verification output, and `git diff <base>..HEAD`.
- Omit implementation chat history and worker prose so the reviewer evaluates the diff cold.
- Route findings from the blind reviewer through the same `auto-fix` (`drive.py resume`, max 2 rounds) and `ask-user` triage rules in [`../SKILL.md`](../SKILL.md).

## 4. Escalation and User Communication

When an `ask-user` item arises, present clear options and trade-offs directly or via `ask_question`. Never treat silence or an unanswered prompt as approval to proceed or skip. Rewrite final summaries cleanly for the user without dumping raw JSONL paths or internal tool flags.
