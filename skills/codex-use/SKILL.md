---
name: codex-use
description: Drive headless coding workers (Codex CLI and DeepSeek Harness) from any shell-capable agent host with isolated git worktrees, anti-over-engineering diff gates, and real-command JSONL verification.
---

# codex-use

Delegate bounded `scout`, `ship`, or `review` slices to local headless CLIs (`codex`, `dsh`) via [`scripts/drive.py`](scripts/drive.py).

## 1. CLI Quick Reference (Black-Box Rule)

Invoke [`scripts/drive.py`](scripts/drive.py) strictly as a black-box CLI. Never open `scripts/drive.py` source with `read_file`; run `python3 .../drive.py <subcmd> --help` if needed.

| Subcommand | Canonical flags | Behavior |
| --- | --- | --- |
| `preflight` | `--worker <codex\|dsh> --cwd <repo>` | Standalone check (`scout`). |
| `worktree create` | `--repo <r> --task <slug> --preflight --worker <w> [--base <ref>]` | Preflights first, creates linked worktree + `base_sha`, auto-excludes `.codex-use/`. |
| `run` | `--worker <w> --mode <scout\|ship> --cwd <d> --prompt-file <f> [--effort medium\|high\|xhigh] [--schema <s>] [--image <p>...] [--base <sha>] [--full-output]` | Returns compact `AuditSummary` (120-char single-line commands; omits `output_preview` on `exit_code == 0` and duplicate `final_message` when `receipt_valid` is true; 160-char failure previews; verbatim stream in `raw_log` and `--full-output`). |
| `resume` | `--session <id>` + same flags as `run` | Continues same thread under exclusive `ship` lock. |
| `review` | `--worker codex --cwd <d> [--base <br> \| --uncommitted \| --commit <sha>] [--prompt-file <f>] [--full-output]` | Non-interactive diff review (`dsh` exits `1`). |
| `audit` | `--worker <w> --log <jsonl> [--cwd <d>] [--base <sha>] [--full-output]` | Re-parses JSONL into `AuditSummary`. |
| `worktree remove` | `--repo <r> --worktree <wt> [--delete-branch] [--force]` | Auto-cleans safe caches (`.codex-use`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.DS_Store`) and prunes empty `.worktrees` dirs; fails closed on real untracked/modified files. |

## 2. Boundaries and Profiles

- **Host:** owns scope, architecture, worktree setup, diff audit, independent verification, and user replies (rewritten for a first-time reader; zero internal shorthand or raw JSONL).
- **Worker:** owns one headless slice. Never run concurrent `ship` slices in the same worktree or on overlapping files (`flock` exits `3` on collision).
- Load only matching profiles:
  - Workers: [`workers/codex.md`](workers/codex.md), [`workers/dsh.md`](workers/dsh.md)
  - Hosts: [`hosts/antigravity.md`](hosts/antigravity.md), [`hosts/grok-bot.md`](hosts/grok-bot.md), [`hosts/dsh.md`](hosts/dsh.md)
  - Templates: [`templates/scout-task.md`](templates/scout-task.md), [`templates/ship-task.md`](templates/ship-task.md), [`templates/bugfix-task.md`](templates/bugfix-task.md)
  - Schema: [`schemas/receipt.schema.json`](schemas/receipt.schema.json)

## 3. Modes

- **`scout`:** read-only tracing/repro/planning; no worktree, edits, push, or PR; exhaustive single-turn check with reversible assumptions logged; promote approved findings straight into `ship`.
- **`ship`:** isolated writes following Section 4.
- **`review`:** `drive.py review --worker codex` or blind-context host diff review.

## 4. Three-Phase `ship` Gate

**Phase 1 (Host setup).** Lock goal, non-goals, core types/interfaces, and one canonical verification command. Run `worktree create --repo <repo> --task <slug> --preflight --worker <w>` and record `worktree` and `base_sha`.

**Phase 2 (Worker execution).** Write the brief from [`templates/ship-task.md`](templates/ship-task.md) or [`templates/bugfix-task.md`](templates/bugfix-task.md) into `<worktree>/.codex-use/<task>.md` (auto-excluded in `<git_common_dir>/info/exclude`, never touching `.gitignore`) and call `drive.py run` with `--schema skills/codex-use/schemas/receipt.schema.json`. Every `ship` prompt includes five blocks (`scout` omits write authorization):

1. **Finish-the-slice contract:** isolated worktree + baseline commit; complete all authorized steps in one turn using reversible assumptions instead of stopping at "suggested next steps"; no extra worktrees, pushes, PRs, or irreversible external actions.
2. **Ban list:** no unrequested abstractions, compatibility shims, defensive error swallowing, narrating comments, or code-golfing (abbreviated identifiers or dense one-liners faking brevity).
3. **Budget and verification:** quantitative file/line scope budget (not readability compression), review rubric, and one canonical verification command.
4. **Resource etiquette:** bound build/test concurrency, avoid focus stealing, clean up background processes and temp files.
5. **Receipt:** self-contained JSON matching [`schemas/receipt.schema.json`](schemas/receipt.schema.json).

**Phase 3 (Host audit and triage).** Never trust worker prose alone. Run all three checks:

1. **Log audit:** verify `AuditSummary.commands` exit codes, `errors`, and `receipt_valid` (`--full-output` or `raw_log` only when truncated).
2. **Diff audit:** inspect `git -C <wt> diff <base_sha>..HEAD` and `git status --short` against the ban list, diff budget, readability, and prose/UI preservation rules.
3. **Local verification:** run the canonical verification command yourself in the worktree.

Triage findings:

- **`auto-fix` (same-thread resume, max 2 rounds):** correctness, security, edge cases, failing tests, code-golfing, or over-engineering. Resume with exact `file:line` fixes and re-run all three checks. Cap automatic rework at **2 `drive.py resume` rounds**; if checks still fail after 2 rounds, halt and escalate the blocker, current `git diff`, and verification output to the user.
- **`ask-user` (escalate and pause):** product behavior, external contracts, irreversible actions, or out-of-scope tradeoffs. Present options, impact, and a recommendation. **User silence or timeout is never consent, rejection, or permission to skip.**

Run a blind-context review (goal, rubric, `git diff` only) for high-risk or cross-module diffs. Clean up via `drive.py worktree remove --repo <repo> --worktree <wt> --delete-branch`.
