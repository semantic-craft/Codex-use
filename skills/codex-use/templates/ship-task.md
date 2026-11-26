Dispatched as a subagent for a bounded slice; skip workflow-routing skills such as using-ryugi and execute the task directly.

# Ship Task Brief

**Workspace:** isolated git worktree `<worktree-path>` on `<base-sha>`. Direct edits and local verify are authorized.

**Contract:** finish all steps in this turn, log reversible assumptions in the receipt, and never stop at "suggested next steps." No extra worktrees, pushes, or PRs.

## Objective, Non-Goals, and Reuse

- **Objective:** `<concrete change>`
- **Non-goals:** `<out-of-scope behavior/files>`
- **Targets:** `<path/to/file.py>` (reuse `<symbol>`), `<path/to/test_file.py>`

## Ban List and Verification

- No unrequested abstractions, shims, error swallowing, narrating comments, or code-golfing.
- **Budget:** <= `<N>` files, ~`<M>` net lines.
- **Verify:** `<single command>` (bound concurrency; avoid focus stealing; clean up temp processes/files).

## Receipt (`schemas/receipt.schema.json`)

Return JSON: `summary`, `changed_files`, `verification` (commands + real exit codes), `assumptions_and_risks`, `symbol_changes`.
