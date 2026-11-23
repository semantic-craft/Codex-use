# Bugfix Task Brief

**Workspace:** isolated git worktree `<worktree-path>` on baseline `<base-sha>`. Direct edits and local verification are authorized.

**Contract:** reproduce the failure first, trace root cause, apply the minimal fix, and verify in this turn. Make reversible assumptions for minor unspecified details and log them. Never stop at "suggested next steps." Do not create extra worktrees, push, or open PRs.

## Bug and Entry Points

- **Observed failure:** `<exact error or wrong behavior>`
- **Expected behavior:** `<exact contract>`
- **Repro command:** `<command that fails before fix>`
- **Entry points:** `<path/to/module.py>:<line>`, `<path/to/test_module.py>`

## Ban List and Verification

- Fix root cause only; no error swallowing, silent fallbacks, unrequested abstractions, compatibility shims, narrating comments, or code-golfing. Leave unrelated files untouched.
- **Budget:** <= `<N>` files, ~`<M>` net lines.
- **Regression test:** one focused test failing before fix and passing after.
- **Verify command:** `<single test command>` (bound concurrency; clean up spawned processes/temp files).

## Receipt (`schemas/receipt.schema.json`)

Return self-contained JSON: `summary`, `changed_files`, `verification` (repro before/after + verify command with real exit codes), `assumptions_and_risks`, `symbol_changes`.
