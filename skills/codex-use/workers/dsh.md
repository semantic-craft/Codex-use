# Worker Profile: DeepSeek Harness (`--worker dsh`)

Load this profile when invoking `scripts/drive.py` with `--worker dsh`. Keep Codex-only effort tiers and `"use subagents as needed"` hints out of DSH prompts.

## 1. CLI Contract (`dsh --profile headless`)

- `scout`: `dsh --profile headless --permission-mode read-only --approval-policy never --json -` (subprocess `cwd`).
- `ship`: `dsh --profile headless --permission-mode workspace-write --approval-policy never [--add-dir <git_common_dir>] --json -` (exclusive worktree lock + auto `--add-dir` for linked worktrees).
- `resume`: `dsh --profile headless --session-id <id> --permission-mode <mode> --approval-policy <policy> [--add-dir <git_common_dir>] --json -`.
- `review`: unsupported natively (`drive.py review --worker dsh` exits `1` and recommends host diff audit or `--worker codex`).

## 2. Safety Guards and Worktree Access

- `danger-full-access` exits `2` unless `--allow-danger` is passed explicitly.
- `--image` exits `2` (never silently dropped) because `dsh --profile headless` does not support image attachments; use `--worker codex` for visual slices.
- Under `workspace-write`, `DshAdapter` runs `git -C <cwd> rev-parse --git-common-dir` and appends `--add-dir <common_dir>` automatically whenever shared Git metadata lies outside `<cwd>`, enabling `git` operations in linked worktrees without widening permissions.

## 3. Credentials and Receipts

`dsh --profile headless` requires `DEEPSEEK_API_KEY` or the headless credentials patch in `~/.dsh/profiles/headless/cordis.patch.yml`. Missing credentials surface as `MISSING_CREDENTIAL` in `AuditSummary.errors`. Because DSH has no native `--output-schema` flag, include the receipt JSON shape in the prompt when you want `AuditSummary.receipt` parsed and validated.
