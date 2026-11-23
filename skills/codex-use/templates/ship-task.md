# Ship Task Brief

## Workspace and Finish-the-Slice Contract

You are operating inside an isolated git worktree at `<worktree-path>` backed by baseline commit `<base-sha>`. You are authorized to edit target files directly and run verification commands locally.

Keep advancing while authorized next steps remain. When minor details are unspecified, make a reversible assumption, record it in the receipt, and finish the slice in this turn. Never end the turn with "suggested next steps" or routine confirmation requests. Do not create extra worktrees, push branches, open pull requests, or perform irreversible actions outside this worktree.

## Objective

<State the concrete change to implement or refactor.>

## Explicit Non-Goals

- <Anything out of scope that must stay untouched.>

## Target Files and Existing Symbols to Reuse

- `<path/to/file.py>`: reuse `<existing_class_or_helper>`
- `<path/to/test_file.py>`: add regression coverage only for the new behavior or bug gap

## Explicit Ban List

- No unrequested abstractions, wrapper classes, or speculative extensibility layers.
- No compatibility shims or dead fallback paths.
- No defensive error swallowing or empty catch blocks.
- No narrating comments that restate what code does.
- No code-golfing (shortened identifiers or dense one-liners) to fake a smaller diff.

## Scope Budget and Verification

- **Diff budget (scope guard, not readability compression):** at most `<N>` files and ~`<M>` net lines.
- **Canonical verification command:** `<single command, e.g., python3 -m unittest discover -s tests -v>`
- **Resource etiquette:** keep test/build parallelism bounded, do not steal desktop focus, and stop any background processes or temporary files created during this run.

## Completion Receipt

Write a self-contained final response for a reader seeing the outcome for the first time, matching `schemas/receipt.schema.json`:

- `summary`: plain-language outcome without internal shorthand or dangling references.
- `changed_files`: repository-relative paths created, modified, or deleted.
- `verification`: commands actually executed with real exit codes and outcome notes.
- `assumptions_and_risks`: reversible assumptions made plus remaining risks or uncovered boundaries.
- `symbol_changes`: key symbols added, modified, or removed.
