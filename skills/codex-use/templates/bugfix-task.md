Dispatched as a subagent for a bounded slice; skip workflow-routing skills such as using-ryugi and execute the task directly.

# Bugfix Task Brief

**Workspace:** isolated git worktree `<worktree-path>` on `<base-sha>`. Direct edits and local verification are authorized.

**Contract:** reproduce first, trace root cause, apply minimal fix, and verify in this turn. Log reversible assumptions; never stop at "suggested next steps." Do not create extra worktrees, push, or open PRs.

## Bug and Entry Points

- **Observed failure:** `<exact error or wrong behavior>`
- **Expected behavior:** `<exact contract>`
- **Repro command:** `<command that fails before fix>`
- **Entry points:** `<path/to/module.py>:<line>`, `<path/to/test_module.py>`

## Ban List and Verification

- Fix root cause only; no error swallowing, silent fallbacks, unrequested abstractions, shims, narrating comments, or code-golfing.
- **Budget:** <= `<N>` files, ~`<M>` net lines.
- **Regression test & verify:** `<single test command>` failing before fix and passing after (bound concurrency; clean up processes/temp files).

## Receipt (`schemas/receipt.schema.json`)

Return JSON: `summary`, `changed_files`, `verification` (repro + verify with real exit codes), `assumptions_and_risks`, `symbol_changes`.
