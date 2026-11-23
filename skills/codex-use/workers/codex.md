# Worker Profile: Codex CLI (`--worker codex`)

Read this profile when running `scripts/drive.py` with `--worker codex`. Keep Codex-specific prompting rules here so they never leak into other worker adapters.

## 1. CLI Contract (Measured on `codex-cli 0.171.0`)

`scripts/drive.py` constructs exact official CLI commands and parses JSONL events into a unified `AuditSummary`.

| Operation | Underlying CLI invocation | Notes |
| --- | --- | --- |
| `scout` | `codex exec -s read-only -C <cwd> -c model_reasoning_effort="<effort>" --json -` | Prompt arrives on stdin to avoid shell quoting bugs. |
| `ship` | `codex exec -s workspace-write -C <cwd> -c model_reasoning_effort="<effort>" --json [--output-schema <schema>] -` | Acquires exclusive worktree lock before launch. |
| `resume` | `codex exec resume <session_id> --json [-c model_reasoning_effort="<effort>"] -` | Never passes `-s` or `-C` (rejected by `codex exec resume`); sets subprocess `cwd` instead. |
| `review` | `codex exec review --json [--base <branch> \| --uncommitted \| --commit <sha>] [-]` | Runs in subprocess `cwd`. |

## 2. Reasoning Effort Tiers (`ultra` Is Forbidden)

`CodexAdapter` enforces a strict allowlist (`medium`, `high`, `xhigh`) before spawning any subprocess. Passing `ultra` or an unrecognized value exits immediately with code `2`.

| Task depth | `--effort` | Typical scope |
| --- | --- | --- |
| Quick reconnaissance or narrow patch | `medium` | Single-module edit, targeted bug fix, or light read-only check with explicit boundary files. |
| Cross-module tracing or full verification | `high` | Multi-file root-cause trace, interface updates, or long verification chains. |
| Deep multi-factor analysis | `xhigh` | Concurrency bugs, state-machine design, or complex system failures requiring hypothesis elimination. |

## 3. Prompting Rules for Codex Models (GPT-6.2 Family)

Apply these rules when writing `--prompt-file` payloads for `--worker codex`:

1. **Write like a brief to a senior teammate.** Point straight to target files, existing types, and observed behavior. Instruct Codex to reuse existing classes, helpers, and APIs instead of inventing parallel structures, and to preserve caller behavior unless the task explicitly changes it. Add new tests only when requested, required by repo rules, or needed to cover a real regression gap.
2. **Prevent premature stopping.** Codex tends to pause at minor ambiguities and return a list of "suggested next steps." State clearly in the prompt that authorized next steps must be executed to completion in the same turn, with reversible assumptions documented in the receipt.
3. **Do not add vague "be concise" instructions.** Default Codex prose is already compact; adding generic brevity instructions degrades responses into telegraphic fragments. Instead require accurate domain terminology, active voice, front-loaded conclusions, and a self-contained final answer written for a reader who did not see intermediate tool steps.
4. **Grant subagents selectively.** Include `"use subagents as needed"` only when the slice has clean read-only or non-overlapping parallel sub-problems. Omit it for single-track edits or shared write targets.
5. **Preserve prefix discipline on `resume`.** When looping on `auto-fix`, resume the same thread (`drive.py resume --session <id>`) with small, concrete `file:line` corrections rather than restarting from scratch or switching models mid-slice. Reinforce clean work explicitly so the model does not over-correct and rewrite sound code.
6. **UI and visual slices.** Specify concrete visual style and typography up front. Strip decorative badges, redundant labels, and text that merely restates visible UI state. Require real-size rendering and interactive path verification rather than source inspection alone.
7. **Keep project `AGENTS.md` lean.** Put only repository conventions that cannot be inferred from code.
