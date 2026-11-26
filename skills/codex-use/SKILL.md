---
name: codex-use
description: Drive headless coding workers (Codex CLI, DeepSeek Harness), dispatch warm Layer A daemon runs, or attach to live terminal Codex TUIs with isolated git worktrees, diff gates, and JSONL command verification.
---

# codex-use

Delegate `scout`, `ship`, or `review` slices to headless CLIs (`codex`, `dsh`), warm Layer A daemon threads (`run --daemon`, `attach --create`), or live `codex` TUIs via [`scripts/drive.py`](scripts/drive.py).

- **Use `codex-use`:** repo `scout`/`ship`/`review` slices with worktree + diff gates, warm Layer A daemon runs (`run --daemon`, `attach --create`), `--fast` priority turns, or live TUI `attach`.
- **Use `codex-driver` (if installed):** MCP-managed multi-session fleets, persistent plans/goals, or interactive remote steering/approvals.

## 1. CLI Quick Reference (Black-Box Rule)

Run [`scripts/drive.py`](scripts/drive.py) as a black-box CLI. Never open `scripts/drive.py` with `read_file`; use `drive.py <subcmd> --help`.

| Subcommand | Canonical flags | Behavior |
| --- | --- | --- |
| `preflight` | `--worker <codex\|dsh> --cwd <repo>` | Standalone check (`scout`). |
| `worktree create` | `--repo <r> --task <slug> --preflight --worker <w> [--base <ref>]` | Preflights, creates linked worktree + `base_sha`, excludes `.codex-use/`. |
| `run` | `--worker <w> --mode <scout\|ship> --cwd <d> --prompt-file <f> [--daemon [--keep-loaded] [--no-auto-daemon]] [--fast] [--with-mcp] [--raw-prompt] [--model <id>] [--effort medium\|high\|xhigh] [--schema <s>] [--image <p>...] [--base <sha>] [--full-output]` | Compact `AuditSummary` (120-char commands; omits `output_preview` on `0` and `final_message` when `receipt_valid`; `raw_log`). `--daemon` uses warm Layer A daemon and archives unless `--keep-loaded`. |
| `resume` | `--session <id>` + `run` flags | Continues thread under `ship` lock (`--fast`, `--with-mcp`, `--raw-prompt`). |
| `sessions` | `[--cwd <d>] [--ensure-daemon] [--remote <url>]` | Lists Layer A threads and Layer B locks (`--ensure-daemon` starts daemon). |
| `attach` | `--cwd <d> [--session <id> \| --latest \| --list \| --create] [--no-auto-daemon] [--fast] [--raw-prompt] [--mode scout\|ship] --prompt-file <f> [--schema <s>] [--timeout <s>] [--wait-for-approval] [--remote <url>]` | Pairs via `codex-backend`. `--create` starts local daemon and boots a warm thread on `not_found` (never `ambiguous` or `desktop_writer_lock`). |
| `review` | `--worker codex --cwd <d> [--model <id>] [--base <br> \| --uncommitted \| --commit <sha>] [--prompt-file <f>]` | Non-interactive diff review (`dsh` exits `1`). |
| `audit` | `--worker <w> --log <jsonl> [--cwd <d>] [--base <sha>] [--full-output]` | Re-parses `exec`, `daemon`, or `attach` JSONL into `AuditSummary`. |
| `worktree remove` | `--repo <r> --worktree <wt> [--delete-branch] [--force]` | Cleans safe caches and empty `.worktrees`; fails closed on dirty files. |

## 2. Boundaries and Profiles

- **Host:** owns scope, worktrees, diff audit, local verify, and clean replies (no raw JSONL/shorthand).
- **Worker:** owns one slice (`run`/`resume`) or daemon/TUI turn (`run --daemon`/`attach`). Never overlap `ship` writes (`flock` exits `3`). Prepends subagent preamble unless `--raw-prompt`.
- **Layer A `attach`:** exits `2` on `--cwd` ambiguity and `3` on Layer B locks; never auto-approves (`approval_required`/`user_input_required` unless `--wait-for-approval`).
- Profiles: [`workers/codex.md`](workers/codex.md), [`workers/dsh.md`](workers/dsh.md), [`hosts/antigravity.md`](hosts/antigravity.md), [`hosts/grok-bot.md`](hosts/grok-bot.md), [`hosts/dsh.md`](hosts/dsh.md), [`templates/scout-task.md`](templates/scout-task.md), [`templates/ship-task.md`](templates/ship-task.md), [`templates/bugfix-task.md`](templates/bugfix-task.md), [`schemas/receipt.schema.json`](schemas/receipt.schema.json).

## 3. Modes

- **`scout`:** read-only tracing/repro/planning; no worktree, edits, push, or PR; promote findings into `ship`.
- **`ship`:** isolated writes per Section 4.
- **`review`:** `drive.py review --worker codex` or blind-context host diff review.

## 4. Three-Phase `ship` Gate

**Phase 1 (Setup).** Lock goal, non-goals, core types, and one verify command. Run `worktree create --repo <repo> --task <slug> --preflight --worker <w>` for `worktree` + `base_sha`.

**Phase 2 (Execution).** Draft from [`templates/ship-task.md`](templates/ship-task.md) or [`templates/bugfix-task.md`](templates/bugfix-task.md) in `<worktree>/.codex-use/<task>.md` (`<git_common_dir>/info/exclude`, never `.gitignore`) and run `drive.py run` (`--daemon`/`--fast`) with `--schema skills/codex-use/schemas/receipt.schema.json`:

1. **Finish-the-slice:** complete all steps in one turn with reversible assumptions logged; no extra worktrees, pushes, PRs, or side effects.
2. **Ban list:** no unrequested abstractions, shims, error swallowing, narrating comments, or code-golfing.
3. **Budget & etiquette:** file/line budget, one verify command, bounded concurrency, no focus stealing, clean up temp processes/files.
4. **Receipt:** JSON matching [`schemas/receipt.schema.json`](schemas/receipt.schema.json).

**Phase 3 (Audit and triage).** Check: (1) `AuditSummary.commands` exit codes, `errors`, `receipt_valid`; (2) `git -C <wt> diff <base_sha>..HEAD` + `git status --short` vs ban list, budget, readability, and UI/prose; (3) run verify command locally in `<wt>`.

- **`auto-fix` (max 2 rounds):** correctness, security, edge cases, failing tests, code-golfing, or over-engineering. Cap rework at **2 `drive.py resume` rounds**; escalate blocker, `git diff`, and test output if still failing.
- **`ask-user` (pause):** product behavior, external contracts, irreversible actions, or scope tradeoffs. **Silence or timeout is never consent, rejection, or permission to skip.**

Use blind-context review (`git diff` + goal + rubric) on high-risk diffs. Clean up via `drive.py worktree remove --repo <repo> --worktree <wt> --delete-branch`.
