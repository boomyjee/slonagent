---
name: diary-enrichment-feature-or-fix
description: Workflow command scaffold for diary-enrichment-feature-or-fix in slonagent.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /diary-enrichment-feature-or-fix

Use this workflow when working on **diary-enrichment-feature-or-fix** in `slonagent`.

## Goal

Implements new features, bugfixes, or UI changes for the diary enrichment script, often iteratively in small commits.

## Common Files

- `scripts/diary_enchiment/__main__.py`
- `scripts/diary_enchiment/validate.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Edit scripts/diary_enchiment/__main__.py to implement feature or fix.
- Optionally edit scripts/diary_enchiment/validate.py for validation-related changes.
- Commit with a message prefixed by feat(diary), fix(diary), ui(diary), or prompt(diary).

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.