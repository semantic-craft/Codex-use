# Worker Profile: Codex CLI (`--worker codex`)

Load for `--worker codex`. Keep Codex-specific rules isolated here.

## 1. CLI Contract (`codex-cli 0.171.0`)

- `scout`: `codex exec -s read-only -C <cwd> -c model_reasoning_effort="<effort>" --json [--image <p>...] -`
- `ship`: `codex exec -s workspace-write -C <cwd> -c model_reasoning_effort="<effort>" --json [--output-schema <s>] [--image <p>...] -`
- `resume`: `codex exec resume <id> --json [-c model_reasoning_effort="<effort>"] [--image <p>...] -` (never passes `-s`/`-C`; uses subprocess `cwd`)
- `review`: `codex exec review --json [--base <br> | --uncommitted | --commit <sha>] [-]`
- **Desktop standalone fallback:** checks `PATH` first, then executable `~/.codex/packages/standalone/current/bin/codex`.
- **Images:** repeatable `--image <path>` validates files exist before spawn (exit `2` if missing).

## 2. Reasoning Effort (`ultra` Forbidden)

`CodexAdapter` enforces `effort in {"medium", "high", "xhigh"}` before spawn (`ultra`/unknown exits `2`):

| `--effort` | Scope |
| --- | --- |
| `medium` | Single-module patch or quick read-only scout with explicit boundary files. |
| `high` | Cross-module tracing, interface updates, or full verification chains. |
| `xhigh` | Concurrency bugs, state machines, or multi-factor root-cause analysis. |

## 3. GPT-6.2 Prompting Rules

1. **Teammate brief + reuse:** point directly to files, symbols, and observed behavior; reuse existing classes/helpers/APIs and preserve caller behavior; add tests only when requested, required by repo rules, or closing a regression gap.
2. **Anti-early-stop:** require finishing all authorized steps in one turn with reversible assumptions logged, never ending at "suggested next steps."
3. **No vague "be concise":** default output is already tight; require accurate domain terms, active voice, front-loaded conclusions, and a self-contained final answer.
4. **Selective subagents:** include `"use subagents as needed"` only for clean parallel read-only or non-overlapping sub-tasks; omit for single-track or shared writes.
5. **Prefix discipline on `resume`:** loop `auto-fix` in the same session with small `file:line` edits; affirm sound work explicitly so the model does not over-correct.
6. **UI slices:** specify style/typography up front, strip filler badges/redundant labels, and require real-size rendering plus interactive path checks.
7. **Lean `AGENTS.md`:** record only conventions that cannot be inferred from code.
