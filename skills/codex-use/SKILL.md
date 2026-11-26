---
name: codex-use
description: Drive headless coding workers (Codex CLI and DeepSeek Harness) or attach to live terminal Codex TUI sessions with isolated git worktrees, diff gates, and JSONL command verification.
---

# codex-use

Delegate `scout`, `ship`, or `review` slices to headless CLIs (`codex`, `dsh`) or pair with a live terminal `codex` TUI via [`scripts/drive.py`](scripts/drive.py).

## 1. CLI Quick Reference (Black-Box Rule)

Invoke [`scripts/drive.py`](scripts/drive.py) as a black-box CLI. Never open `scripts/drive.py` with `read_file`; run `python3 .../drive.py <subcmd> --help` if needed.

| Subcommand | Canonical flags | Behavior |
| --- | --- | --- |
| `preflight` | `--worker <codex\|dsh> --cwd <repo>` | Standalone check (`scout`). |
| `worktree create` | `--repo <r> --task <slug> --preflight --worker <w> [--base <ref>]` | Preflights, creates linked worktree + `base_sha`, excludes `.codex-use/`. |
| `run` | `--worker <w> --mode <scout\|ship> --cwd <d> --prompt-file <f> [--model <id>] [--effort medium\|high\|xhigh] [--schema <s>] [--image <p>...] [--base <sha>] [--full-output]` | Returns compact `AuditSummary` (120-char single-line commands; omits `output_preview` on `exit_code == 0` and `final_message` when `receipt_valid`; 160-char failure previews; verbatim `raw_log`). |
| `resume` | `--session <id>` + `run` flags | Continues thread under exclusive `ship` lock. |
| `sessions` | `[--cwd <d>] [--remote <url>]` | Lists loaded Layer A TUI threads and Desktop/IDE locks. |
| `attach` | `--cwd <d> [--session <id> \| --latest \| --list] [--mode scout\|ship] --prompt-file <f> [--schema <s>] [--timeout <s>] [--wait-for-approval] [--remote <url>]` | Attaches via `codex-backend` without stealing locks, queues/starts turn, streams `AuditSummary`, unsubscribes on exit. |
| `review` | `--worker codex --cwd <d> [--model <id>] [--base <br> \| --uncommitted \| --commit <sha>] [--prompt-file <f>]` | Non-interactive diff review (`dsh` exits `1`). |
| `audit` | `--worker <w> --log <jsonl> [--cwd <d>] [--base <sha>] [--full-output]` | Re-parses `exec` or `attach` JSONL into `AuditSummary`. |
| `worktree remove` | `--repo <r> --worktree <wt> [--delete-branch] [--force]` | Cleans safe caches (`.codex-use`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.DS_Store`) and empty `.worktrees` dirs; fails closed on dirty files. |

## 2. Boundaries and Profiles

- **Host:** owns scope, worktrees, diff audit, local verification, and clean user replies (no internal shorthand or raw JSONL).
- **Worker:** owns one headless slice (`run`/`resume`) or live TUI turn (`attach`). Never overlap `ship` writes (`flock` exits `3`).
- **Live TUI `attach`:** zero-copy-paste Layer A daemon pairing. Exits `2` on multi-session `--cwd` ambiguity and `3` on Desktop/VS Code Layer B locks. Passive observer: never auto-approves; returns `status="approval_required"` / `"user_input_required"` unless `--wait-for-approval` is set.
- Profiles: [`workers/codex.md`](workers/codex.md), [`workers/dsh.md`](workers/dsh.md), [`hosts/antigravity.md`](hosts/antigravity.md), [`hosts/grok-bot.md`](hosts/grok-bot.md), [`hosts/dsh.md`](hosts/dsh.md), [`templates/scout-task.md`](templates/scout-task.md), [`templates/ship-task.md`](templates/ship-task.md), [`templates/bugfix-task.md`](templates/bugfix-task.md), [`schemas/receipt.schema.json`](schemas/receipt.schema.json).

## 3. Modes

- **`scout`:** read-only tracing/repro/planning; no worktree, edits, push, or PR; single-turn check with reversible assumptions logged; promote findings into `ship`.
- **`ship`:** isolated writes per Section 4.
- **`review`:** `drive.py review --worker codex` or blind-context host diff review.

## 4. Three-Phase `ship` Gate

**Phase 1 (Setup).** Lock goal, non-goals, core types, and one verify command. Run `worktree create --repo <repo> --task <slug> --preflight --worker <w>` to record `worktree` and `base_sha`.

**Phase 2 (Execution).** Draft from [`templates/ship-task.md`](templates/ship-task.md) or [`templates/bugfix-task.md`](templates/bugfix-task.md) in `<worktree>/.codex-use/<task>.md` (excluded via `<git_common_dir>/info/exclude`, never `.gitignore`) and run `drive.py run` with `--schema skills/codex-use/schemas/receipt.schema.json`. Include five blocks (`scout` omits write auth):

1. **Finish-the-slice:** complete all authorized steps in one turn using reversible assumptions instead of "suggested next steps"; no extra worktrees, pushes, PRs, or external side effects.
2. **Ban list:** no unrequested abstractions, shims, error swallowing, narrating comments, or code-golfing.
3. **Budget and verify:** file/line budget, rubric, and one verify command.
4. **Resource etiquette:** bound concurrency, avoid focus stealing, clean up processes/temp files.
5. **Receipt:** JSON matching [`schemas/receipt.schema.json`](schemas/receipt.schema.json).

**Phase 3 (Audit and triage).** Run all three checks:

1. **Log audit:** verify `AuditSummary.commands` exit codes, `errors`, and `receipt_valid`.
2. **Diff audit:** inspect `git -C <wt> diff <base_sha>..HEAD` and `git status --short` against the ban list, budget, readability, and UI/prose preservation.
3. **Local verify:** run the verify command yourself in `<wt>`.

Triage:

- **`auto-fix` (same-thread, max 2 rounds):** correctness, security, edge cases, failing tests, code-golfing, or over-engineering. Resume with `file:line` fixes and re-run all three checks. Cap rework at **2 `drive.py resume` rounds**; if still failing, escalate blocker, `git diff`, and test output.
- **`ask-user` (pause):** product behavior, external contracts, irreversible actions, or scope tradeoffs. Present options, impact, and recommendation. **Silence or timeout is never consent, rejection, or permission to skip.**

Use blind-context review (`git diff` + goal + rubric only) on high-risk diffs. Clean up with `drive.py worktree remove --repo <repo> --worktree <wt> --delete-branch`.
