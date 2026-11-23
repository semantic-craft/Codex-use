# Worker Profile: DeepSeek Harness (`--worker dsh`)

Read this profile when running `scripts/drive.py` with `--worker dsh`. Do not copy Codex-only effort tiers or `"use subagents as needed"` hints into DSH prompts.

## 1. CLI Contract (Measured on `dsh --profile headless`)

| Operation | Underlying CLI invocation | Notes |
| --- | --- | --- |
| `scout` | `dsh --profile headless --permission-mode read-only --approval-policy never --json -` | Subprocess `cwd` is set to `--cwd`. |
| `ship` | `dsh --profile headless --permission-mode workspace-write --approval-policy never [--add-dir <git_common_dir>] --json -` | Acquires exclusive worktree lock and auto-adds shared `.git` metadata dir for linked worktrees. |
| `resume` | `dsh --profile headless --session-id <session_id> --permission-mode <mode> --approval-policy <policy> [--add-dir <git_common_dir>] --json -` | Continues the persisted DSH session with matching sandbox flags. |
| `review` | Unsupported natively | `drive.py review --worker dsh` exits `1` and recommends host-side `git diff` audit or `--worker codex`. |

## 2. Permission Modes and Safety Guard

`DshAdapter` validates `--permission-mode` and `--approval-policy` before launching `dsh`:

- `scout` defaults to `--permission-mode read-only --approval-policy never`.
- `ship` defaults to `--permission-mode workspace-write --approval-policy never`.
- `danger-full-access` is rejected with exit code `2` unless `--allow-danger` is explicitly supplied. Avoid it in normal worktree workflows.

## 3. Linked Git Worktree `.git` Access

A linked git worktree points its `.git` file at `<main-repo>/.git/worktrees/<task>`. Under `workspace-write`, DSH restricts filesystem writes to `cwd` unless shared Git metadata is explicitly granted.

`DshAdapter` resolves `git -C <cwd> rev-parse --git-common-dir` automatically whenever `permission-mode` is `workspace-write`. If the common Git directory lies outside `<cwd>`, `drive.py` appends `--add-dir <resolved_common_dir>` on both `run` and `resume` so `git status`, `git diff`, and `git commit` succeed inside linked worktrees without widening permissions to `danger-full-access`.

## 4. Credentials and NDJSON Auditing

`dsh --profile headless` requires either `DEEPSEEK_API_KEY` in the environment or the headless credentials patch in `~/.dsh/profiles/headless/cordis.patch.yml`. If credentials are missing, DSH emits a structured `MISSING_CREDENTIAL` status/final NDJSON event; `DshAdapter.parse_jsonl` surfaces that exact error in `AuditSummary.errors`.

Because `dsh --profile headless` does not expose a native `--output-schema` flag, include the four-item receipt JSON structure directly in your prompt when you want `drive.py` to populate `AuditSummary.receipt` and `receipt_valid`.
