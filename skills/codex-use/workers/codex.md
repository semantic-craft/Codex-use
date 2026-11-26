# Worker Profile: Codex CLI (`--worker codex`)

Load for `--worker codex`.

## 1. CLI and Daemon Contract (`codex-cli 0.171.0`)

- `exec`: `codex exec -s <read-only|workspace-write> -C <cwd> -c model_reasoning_effort="<effort>" [-c mcp_servers={}] [-c service_tier="priority"] --json [--output-schema <s>] [--image <p>...] -`
- `exec resume`: `codex exec resume <id> --json [-c model_reasoning_effort="<effort>"] [-c mcp_servers={}] [-c service_tier="priority"] [--image <p>...] -` (subprocess `cwd`, never `-s`/`-C`).
- **Isolation & preamble:** `run`/`resume` pass `-c mcp_servers={}` unless `--with-mcp`. `run`/`resume`/`attach` prepend a subagent preamble unless `--raw-prompt`.
- `run --daemon`: uses `$CODEX_HOME/app-server-control/app-server-control.sock` (auto-starting local daemon unless `--no-auto-daemon` or custom `--remote`), runs `thread/start` + `turn/start`, then `thread/unsubscribe` + `thread/archive` unless `--keep-loaded`.
- `sessions` / `attach`: WebSocket (`codex-backend`, pipelined `thread/read`). `attach --create` starts local daemon and boots a warm thread on `not_found` for reuse via `attach --cwd` or `codex resume <id>`. Recovers 0-turn empty rollouts, never auto-approves (`--wait-for-approval` waits), and unsubscribes on exit.
- `review` & binary: `codex exec review --json [--base <br> | --uncommitted | --commit <sha>] [-]`; checks `PATH` then `~/.codex/packages/standalone/current/bin/codex`.

## 2. Effort and Speed (`ultra` / `ultrafast` Forbidden)

`effort in {"medium", "high", "xhigh"}` (`ultra`/`ultrafast` exit `2`). `--fast` sets `service_tier="priority"` (`exec`) or `"serviceTier": "priority"` (daemon):

- `medium`: single-module patch or quick scout with explicit boundary files.
- `high`: cross-module tracing, interface updates, or full verification chains.
- `xhigh`: concurrency bugs, state machines, or multi-factor root-cause analysis.

## 3. GPT-6.2 Prompting Rules

1. **Reuse & tests:** cite exact files/symbols; reuse helpers; add tests only when requested, required, or closing a regression gap.
2. **One-turn finish:** log reversible assumptions; never stop at "suggested next steps."
3. **Clear prose:** domain terms, active voice, front-loaded conclusions, self-contained output.
4. **Subagents & `resume`:** add `"use subagents as needed"` only for parallel non-overlapping work; affirm sound work on `resume` with `file:line` fixes.
5. **UI & `AGENTS.md`:** specify UI style up front without filler badges; keep `AGENTS.md` to non-inferable rules.
