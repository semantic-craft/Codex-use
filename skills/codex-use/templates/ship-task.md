# Ship Task Brief

## Workspace and Contract

Isolated git worktree `<worktree-path>` on baseline commit `<base-sha>`. Direct edits and local verification are authorized.

Finish all authorized steps in this turn. Make reversible assumptions for minor unspecified details and log them in the receipt. Never stop early at "suggested next steps." Do not create extra worktrees, push branches, or open PRs.

## Objective, Non-Goals, and Reuse

- **Objective:** `<concrete change to implement or refactor>`
- **Non-goals:** `<out-of-scope behavior/files to leave untouched>`
- **Targets and reuse:** `<path/to/file.py>` (reuse `<existing_symbol>`), `<path/to/test_file.py>` (regression coverage only where requested or needed)

## Ban List and Verification

- No unrequested abstractions, compatibility shims, defensive error swallowing, narrating comments, or code-golfing.
- **Diff budget (scope guard, not readability compression):** <= `<N>` files, ~`<M>` net lines.
- **Canonical verify command:** `<single verification command>`
- **Resource etiquette:** bound build/test concurrency, avoid stealing desktop focus, and clean up spawned processes or temp files.

## Receipt (`schemas/receipt.schema.json`)

Return self-contained JSON: `summary` (plain-language outcome for a first-time reader), `changed_files`, `verification` (commands actually run with real exit codes), `assumptions_and_risks`, and `symbol_changes`.
