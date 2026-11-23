# Host Guide: Grok Bot

Load this guide when Grok Bot orchestrates `codex-use`.

## 1. Local Execution Boundary (Fail Fast in Cloud)

`codex-use` requires local access to the target git repo and local `codex`/`dsh` binaries. Run `drive.py preflight` (or `worktree create --preflight`) first. If it fails inside a cloud container without local repo or binary access, stop immediately, never recreate the repo in the cloud sandbox, and ask the user to enable **Execution on this computer**.

## 2. Local Audit and Escalation

Run every `worktree create`, `drive.py run`, `git diff <base>..HEAD`, and local verification command on the target machine. Rely on compact `AuditSummary` stdout (raw JSONL stays in temp logs outside the repo; pass `--full-output` only when needed). Escalate `ask-user` decisions with options, impact, and a recommendation; silence or timeout is never consent or permission to skip.
