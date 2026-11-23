# Host Guide: Grok Bot

Read this guide when Grok Bot is the host orchestrating `codex-use`.

## 1. Local Execution Boundary (Fail Fast in Cloud Containers)

`codex-use` requires local filesystem access to the target git repository and local `codex` or `dsh` binaries.

- Run `scripts/drive.py preflight --worker <codex|dsh> --cwd <repo>` as the very first shell command.
- If preflight fails because the session is running inside an isolated cloud container without local repository access or worker binaries, stop immediately. Do not recreate the repository inside the cloud sandbox and do not present cloud-container test runs as local verification.
- Ask the user to enable **Execution on this computer** (or switch to a local machine session) and retry once preflight succeeds.

## 2. Worktree, Diff Audit, and Verification

Execute every `worktree create`, `drive.py run`, `git diff <base>..HEAD`, and local verification command on the target development machine.

- Keep raw JSONL logs in temp directories (`drive.py` default) so they never pollute the repository working tree.
- Inspect the `AuditSummary` JSON returned on stdout instead of reading multi-megabyte raw JSONL logs into context.

## 3. Approval Gates and Escalation

When a worker pauses on a decision that changes product behavior, external contracts, or irreversible state, escalate as an `ask-user` prompt with concrete options, impact, and a recommendation. Never treat timeout or silence as permission to proceed or drop the item.
