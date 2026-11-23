# Scout Task Brief

## Mode and Boundary

Read-only reconnaissance inside `<repo-path>`. Do not edit repository files, create branches, push, or open pull requests.

## Objective

<Describe the exact question, subsystem, or failure to trace.>

## Starting Points

- `<path/to/file.py>` (`<symbol_or_line>`)
- `<path/to/second_file.py>`

## Read-Only Checks to Run

- `<single read-only command, e.g., git log -n 5 -- path/to/file>`
- `<optional reproduction or dry-run test command>`

## Execution Rules

1. Inspect every listed starting point and follow callers/callees exhaustively in a single turn.
2. When minor details are unspecified, make a reasonable reversible assumption, state it explicitly, and finish the analysis instead of pausing to ask routine follow-up questions.
3. Reuse exact repository symbols, file paths, and line numbers. Do not add generic summaries or filler.

## Required Output

Return a self-contained report written for a reader seeing the findings for the first time:

1. **Key findings.** Exact `file:line` references and traced control flow.
2. **Observed command output.** Commands actually run with their exit codes and relevant output lines.
3. **Assumptions and risks.** Reversible assumptions made, open tradeoffs, and edge cases.
4. **Proposed change scope.** Minimal file list and candidate verification command if promoted to `ship`.
