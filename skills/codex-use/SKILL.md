---
name: codex-use
description: Drive headless coding workers (Codex CLI and DeepSeek Harness) from any shell-capable agent host with isolated git worktrees, anti-over-engineering diff gates, and real-command JSONL verification.
---

# codex-use

Use this skill when a host agent delegates bounded reconnaissance, implementation, or diff review slices to a local headless coding worker (`codex` or `dsh`) via [`scripts/drive.py`](scripts/drive.py).

## 1. Host and Worker Boundaries

The architecture splits responsibilities across two axes.

- **Host agent.** Owns scope, architecture, worktree creation, independent verification, diff auditing, and all communication with the user.
- **Headless worker.** Executes one read-only `scout` slice, one isolated `ship` slice, or one `review` pass through its official CLI.
- **Parallel isolation invariant.** Never launch two concurrent `ship` runs against the same worktree or overlapping file sets. Every concurrent write slice gets its own linked git worktree. `scripts/drive.py` enforces a non-blocking exclusive lock per worktree (`exit code 3` on collision).
- **Self-contained user reporting.** Rewrite every milestone report and final answer as if the user is seeing the result for the first time. Do not dump raw JSONL, internal agent shorthand, or dangling continuations of collapsed progress updates.

Load only the profile files matching your active environment.

- Workers: [`workers/codex.md`](workers/codex.md) or [`workers/dsh.md`](workers/dsh.md).
- Hosts: [`hosts/antigravity.md`](hosts/antigravity.md), [`hosts/grok-bot.md`](hosts/grok-bot.md), or [`hosts/dsh.md`](hosts/dsh.md).
- Completion receipt contract: [`schemas/receipt.schema.json`](schemas/receipt.schema.json).

## 2. Operating Modes

Classify every delegation as `scout`, `ship`, or `review` before spawning a worker.

### `scout` (read-only reconnaissance)

Use `scout` for investigation, root-cause tracing, reproduction, or change planning.

- No isolated worktree is required.
- The worker runs in `read-only` mode and must not edit repository files, push branches, or open pull requests.
- Require exhaustive single-turn coverage of the named files and checks. When minor details are unspecified, the worker makes reversible assumptions, states them explicitly, and finishes the investigation instead of pausing mid-way.
- After auditing the worker output, present a self-contained summary to the user. Once approved, carry the `scout` findings directly into `ship` without repeating discovery.

### `ship` (isolated implementation)

Use `ship` for authorized code changes, bug fixes, and refactors. Follow the three-phase gate in Section 3 strictly.

### `review` (non-interactive diff audit)

Use `review` for an independent pass over uncommitted changes, a commit, or a branch diff (`scripts/drive.py review`). Check your active worker profile for native review support; when unavailable, run a blind-context host diff review.

## 3. Three-Phase `ship` Gate

### Phase 1. Host Preparation

Complete all four steps locally before invoking a worker.

1. Lock the goal and explicit non-goals so scope cannot drift.
2. Define core data shapes, interface boundaries, and one canonical verification command.
3. Run preflight to confirm binary availability, credentials, and repository state:

```bash
python3 skills/codex-use/scripts/drive.py preflight --worker codex --cwd /path/to/repo
```

4. Create an isolated linked git worktree and record the returned `base_sha`:

```bash
python3 skills/codex-use/scripts/drive.py worktree create --repo /path/to/repo --task <task-slug>
```

### Phase 2. Worker Execution

Compose a focused prompt file and dispatch through `scripts/drive.py run`.

```bash
python3 skills/codex-use/scripts/drive.py run \
  --worker codex \
  --mode ship \
  --cwd /path/to/worktree \
  --base <base_sha> \
  --schema skills/codex-use/schemas/receipt.schema.json \
  --prompt-file /tmp/slice-prompt.md
```

Every `ship` prompt must include five concrete blocks (`scout` omits write authorization):

1. **Finish-the-slice contract.** State that the worker is inside a dedicated git worktree with a clean baseline commit. Authorize direct edits to target files and execution of verification commands. Instruct the worker to keep advancing while authorized next steps remain, make reversible assumptions for minor unspecified details, and never end a turn with "suggested next steps" or routine confirmation requests. Forbid creating extra worktrees, pushing branches, opening PRs, or running irreversible operations outside the worktree.
2. **Explicit ban list.** Forbid unrequested abstractions, compatibility shims, defensive error swallowing, narrating comments, and code-golfing (shortening identifiers or cramming dense one-liners to fake a smaller diff).
3. **Review rubric and diff budget.** State a quantitative scope budget (affected files or net lines as a scope guard, not a license to compress readability), the concrete review checks, and the single verification command.
4. **Resource etiquette.** Cap build/test concurrency on shared workstations, avoid stealing desktop focus during UI tests, and stop any background processes or temp artifacts created during the run.
5. **Four-item completion receipt.** Require a self-contained final response matching [`schemas/receipt.schema.json`](schemas/receipt.schema.json): `changed_files`, `verification` (commands run with real exit codes), `assumptions_and_risks`, and `symbol_changes`.

### Phase 3. Host Audit and Triage

Never accept worker prose at face value. Complete all three independent checks before merging or reporting success.

1. **Real-command log audit.** Inspect the `AuditSummary` returned by `drive.py` (or re-run `drive.py audit --worker <w> --log <raw_log> --cwd <wt> --base <base_sha>`). Verify that the required commands actually executed with exit code `0` and that `errors` is empty.
2. **Local diff audit.** Run `git -C <wt> diff <base_sha>..HEAD` (plus `git status --short`). Check every changed file against the ban list, diff budget, and readability rules. When editing existing prose or UI copy, confirm original author wording was preserved and redundant filler labels were removed.
3. **Independent local verification.** Run the canonical verification command yourself inside the worktree. Rely on your own observed exit code and output.

Route every audit finding into one of two buckets.

- **`auto-fix` (same-thread resume).** Pure correctness bugs, security issues, missing edge cases, failing tests, code-golfing, or over-engineering. Send a targeted fix prompt back to the same worker thread with exact `file:line` citations and the verification command, then repeat all three checks until clean:

```bash
python3 skills/codex-use/scripts/drive.py resume \
  --worker codex \
  --session <session_id> \
  --mode ship \
  --cwd /path/to/worktree \
  --base <base_sha> \
  --prompt-file /tmp/autofix-prompt.md
```

- **`ask-user` (escalate and pause).** Product behavior choices, external API or schema contract changes, irreversible actions, or tradeoffs outside authorized scope. Present clear options, concrete impact, and a recommendation. **User silence or timeout is never consent, rejection, or permission to skip.** Keep the decision pending until an explicit answer arrives.

For high-risk, security-sensitive, or cross-module changes, run an additional blind-context review before integration. Pass only the task goal, review rubric, and `git diff` without implementation history.

Clean up temporary worktrees after integration:

```bash
python3 skills/codex-use/scripts/drive.py worktree remove --repo /path/to/repo --worktree /path/to/worktree --delete-branch
```
