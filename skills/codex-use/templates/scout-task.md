# Scout Task Brief

## Mode and Scope

Read-only reconnaissance in `<repo-path>`. Do not edit files, create branches, push, or open PRs.

- **Objective:** `<exact question, subsystem, or failure to trace>`
- **Starting points:** `<path/to/file.py>` (`<symbol_or_line>`), `<path/to/second.py>`
- **Read-only checks:** `<read-only command or repro check>`

## Rules and Output

Follow callers/callees exhaustively in one turn. Make reversible assumptions for minor unspecified details and state them instead of pausing. Return a self-contained report for a first-time reader:

1. **Key findings:** exact `file:line` references and traced control flow.
2. **Observed output:** commands run, exit codes, and relevant output lines.
3. **Assumptions and risks:** reversible assumptions, open tradeoffs, and edge cases.
4. **Proposed ship scope:** minimal file list and candidate verification command.
