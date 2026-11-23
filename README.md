# Codex-use

[中文文档](README.zh-CN.md)

**Codex-use** is a standalone open-source agent skill and zero-dependency Python runner (`skills/codex-use/scripts/drive.py`) for driving official headless coding CLIs from any shell-capable agent host.

It enforces isolated git worktrees, concurrency locking, worker-specific CLI safety checks, and real-command JSONL auditing without requiring a custom daemon, proxy server, or MCP control plane.

## Why Codex-use

Delegating coding slices to a headless CLI fails in predictable ways when left to prose instructions alone:

- Passing invalid reasoning effort values (such as `ultra`) or passing `-s`/`-C` to `codex exec resume` causes runtime failures.
- Running `dsh --profile headless --permission-mode workspace-write` inside a linked git worktree breaks `git` commands unless `--add-dir <git-common-dir>` is passed.
- Launching two concurrent edits in the same worktree corrupts diffs.
- Trusting a worker's prose summary misses skipped tests, swallowed errors, and unrequested abstractions.

Codex-use encodes these guardrails directly in `scripts/drive.py` and pairs them with a concise `SKILL.md` workflow.

## Architecture (Host x Worker)

```text
Host (Antigravity / Grok Bot / DSH / any SKILL.md agent)
  -> skills/codex-use/SKILL.md          (worker-agnostic scout/ship/review protocol)
  -> skills/codex-use/scripts/drive.py  (preflight, worktree lock, CLI builder, JSONL auditor)
       ├─ CodexAdapter + workers/codex.md   (codex exec / resume / review)
       └─ DshAdapter   + workers/dsh.md     (dsh --profile headless)
```

- **Self-contained skill folder.** Everything needed at runtime lives inside `skills/codex-use/` (protocol, worker/host profiles, task templates under `templates/`, receipt schema, and `scripts/drive.py`). Copying or symlinking that directory alone preserves all relative links and scripts.
- **Zero third-party dependencies.** `scripts/drive.py` uses Python 3.10+ standard library modules only, and falls back to Codex Desktop's standalone binary (`~/.codex/packages/standalone/current/bin/codex`) when `codex` is not on `PATH`.
- **Clean local specs, compact audits, and bounded retries.** Task specs under `.codex-use/` are auto-excluded in `<git_common_dir>/info/exclude` (never modifying `.gitignore`). `AuditSummary` stdout defaults to compact mode (160-char previews, omitting duplicate `final_message` when `receipt_valid` is true; pass `--full-output` for verbose previews and raw `final_message`). `worktree remove` auto-cleans safe disposable caches (`.codex-use`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.DS_Store`) while failing closed on real untracked or modified files. Automatic `auto-fix` resumes cap at 2 rounds before escalating to the user.

## Installation

### Universal (`~/.agents/skills`)

Symlink the self-contained skill folder so any `SKILL.md`-compatible agent discovers it:

```bash
git clone https://github.com/semantic-craft/Codex-use.git Codex-use
mkdir -p ~/.agents/skills
ln -s "$(pwd)/Codex-use/skills/codex-use" ~/.agents/skills/codex-use
```

### Antigravity

The repository ships a root `plugin.json` manifest targeting Antigravity. Clone the repository into your Antigravity plugins directory (or symlink `skills/codex-use` into `~/.agents/skills/codex-use`).

### Grok Bot

The repository ships `.grok-plugin/plugin.json` pointing `skills` to `./skills` with zero MCP servers required. Install the repository as a local Grok Bot plugin and enable **Execution on this computer** so `drive.py` accesses your local git repository and worker binaries.

### DeepSeek Harness (DSH)

The repository ships `package.json` and `cordis.patch.yml` registering `@deepseek-ai/dsh-skill-filesystem`. Install it as a DSH plugin bundle or overlay it directly:

```bash
dsh --profile headless --patch /path/to/Codex-use/cordis.patch.yml --dump-config
```

## Quickstart

### 1. Preflight check

Verify binary availability, credentials, and repository status:

```bash
python3 skills/codex-use/scripts/drive.py preflight --worker codex --cwd /path/to/repo
python3 skills/codex-use/scripts/drive.py preflight --worker dsh --cwd /path/to/repo
```

### 2. Read-only `scout`

Investigate a codebase without editing files or creating a worktree:

```bash
python3 skills/codex-use/scripts/drive.py run \
  --worker codex \
  --mode scout \
  --effort medium \
  --cwd /path/to/repo \
  --prompt "Trace how request retries are configured and list the exact files and lines."
```

### 3. Isolated `ship`

Start from `skills/codex-use/templates/ship-task.md` or `skills/codex-use/templates/bugfix-task.md` (use `templates/scout-task.md` for `scout`), combine preflight and linked worktree creation in one command, run a scoped change with exclusive lock protection (optionally passing repeatable `--image <path>` flags on `--worker codex`), and inspect the compact `AuditSummary` returned on stdout (add `--full-output` if you want 400-char previews and unelided `final_message`):

```bash
python3 skills/codex-use/scripts/drive.py worktree create \
  --repo /path/to/repo --task fix-retry --preflight --worker codex

python3 skills/codex-use/scripts/drive.py run \
  --worker codex \
  --mode ship \
  --effort high \
  --cwd /path/to/.worktrees/repo/fix-retry \
  --base <base_sha> \
  --image /path/to/mock.png \
  --schema skills/codex-use/schemas/receipt.schema.json \
  --prompt-file /path/to/.worktrees/repo/fix-retry/.codex-use/slice-prompt.md
```

### 4. Same-thread `resume` (`auto-fix`, max 2 rounds)

Continue an existing session when local diff audit or verification finds issues (halt and escalate to the user if checks still fail after 2 rework rounds):

```bash
python3 skills/codex-use/scripts/drive.py resume \
  --worker codex \
  --session <session_id> \
  --mode ship \
  --cwd /path/to/.worktrees/repo/fix-retry \
  --base <base_sha> \
  --prompt "Fix the off-by-one guard in src/retry.py:42 and re-run python3 -m unittest."
```

### 5. Non-interactive `review` and log `audit`

```bash
python3 skills/codex-use/scripts/drive.py review --worker codex --cwd /path/to/worktree --base main
python3 skills/codex-use/scripts/drive.py audit --worker codex --log /tmp/codex-use-logs/run.jsonl --cwd /path/to/worktree --base <base_sha>
```

Remove the worktree once changes are merged:

```bash
python3 skills/codex-use/scripts/drive.py worktree remove --repo /path/to/repo --worktree /path/to/.worktrees/repo/fix-retry --delete-branch
```

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

Licensed under the [Apache License 2.0](LICENSE).
