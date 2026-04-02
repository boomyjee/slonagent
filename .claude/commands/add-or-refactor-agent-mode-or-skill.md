---
name: add-or-refactor-agent-mode-or-skill
description: Workflow command scaffold for add-or-refactor-agent-mode-or-skill in slonagent.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /add-or-refactor-agent-mode-or-skill

Use this workflow when working on **add-or-refactor-agent-mode-or-skill** in `slonagent`.

## Goal

Adds a new agent mode or skill, or refactors an existing one (e.g., moving a script to a mode, grouping skills, or porting scripts).

## Common Files

- `src/modes/*`
- `src/skills/*`
- `agent.py`
- `main.py`
- `.gitignore`
- `scripts/*`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Create or update files in src/modes/ or src/skills/ (new .py files or refactor existing ones)
- Update agent.py and/or main.py to register or handle the new mode/skill
- If porting from scripts/, move or rename files from scripts/ to src/modes/ or src/skills/
- Update .gitignore if new script or config paths are introduced
- Update or add supporting files (e.g., formatting.py, protocol.py, state.py) in the new mode/skill directory

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.