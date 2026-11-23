# Bugfix Task Brief

## Workspace and Finish-the-Slice Contract

You are operating inside an isolated git worktree at `<worktree-path>` backed by baseline commit `<base-sha>`. You are authorized to edit target files directly and run verification commands locally.

Reproduce the failure first, trace the root cause, apply the minimal fix, and verify the fix passes before finishing. Make reversible assumptions for minor unspecified details and record them in the receipt. Never stop early at "suggested next steps." Do not create extra worktrees, push branches, or open pull requests.

## Symptom and Reproduction

- **Observed failure:** `<exact error message, wrong output, or failing behavior>`
- **Expected behavior:** `<exact contract behavior>`
- **Reproduction command:** `<command that currently fails before the fix>`

## Suspected Entry Points

- `<path/to/module.py>:<line>`
- `<path/to/test_module.py>`

## Explicit Ban List

- Fix the root cause; do not paper over symptoms with defensive catches or silent fallbacks.
- No unrequested abstractions, compatibility shims, narrating comments, or code-golfing.
- Keep unrelated files and surrounding formatting untouched.

## Scope Budget and Verification

- **Diff budget:** at most `<N>` files and ~`<M>` net lines.
- **Regression test:** add or extend one focused test that fails without the fix and passes with it.
- **Canonical verification command:** `<single command covering the regression test and existing suite>`
- **Resource etiquette:** bound build/test concurrency and clean up any background processes or temp files created during reproduction.

## Completion Receipt

Return a self-contained final response matching `schemas/receipt.schema.json`:

- `summary`: root cause and verified fix written for a first-time reader.
- `changed_files`: repository-relative paths modified.
- `verification`: reproduction command (before/after) and canonical verification command with real exit codes.
- `assumptions_and_risks`: reversible assumptions made and any remaining edge cases.
- `symbol_changes`: symbols added, modified, or removed.
