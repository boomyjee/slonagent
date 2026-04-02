```markdown
# slonagent Development Patterns

> Auto-generated skill from repository analysis

## Overview

This skill teaches you the core development patterns, coding conventions, and workflows used in the `slonagent` Python codebase. The repository is organized around modular agent modes, skills, and memory providers, with a focus on maintainability and extensibility. It uses conventional commit messages, structured workflows for adding features or refactoring, and a consistent code style. While no specific framework is detected, the project emphasizes clarity and modularity in its architecture.

## Coding Conventions

**File Naming**
- Use `camelCase` for file names.
  - Example: `memoryProvider.py`, `agentMode.py`

**Import Style**
- Prefer **relative imports** within the package.
  - Example:
    ```python
    from .utils import formatMessage
    from ..memory.providers import fact
    ```

**Export Style**
- Use **named exports** (explicitly define what is exported from a module).
  - Example:
    ```python
    __all__ = ["Agent", "Skill"]
    ```

**Commit Messages**
- Follow the **Conventional Commits** standard.
- Prefixes: `feat`, `fix`, `refactor`, `docs`, `chore`
- Example:
  ```
  feat: add recall tool to memory provider
  fix: correct Telegram button callback handling
  ```

## Workflows

### Add or Refactor Agent Mode or Skill
**Trigger:** When you want to introduce a new agent mode, port a script to a mode, or refactor/group skills for better organization.  
**Command:** `/new-mode`

1. Create or update files in `src/modes/` or `src/skills/` (add new `.py` files or refactor existing ones).
2. Update `agent.py` and/or `main.py` to register or handle the new mode/skill.
3. If porting from `scripts/`, move or rename files from `scripts/` to `src/modes/` or `src/skills/`.
4. Update `.gitignore` if new script or config paths are introduced.
5. Update or add supporting files (e.g., `formatting.py`, `protocol.py`, `state.py`) in the new mode/skill directory.

**Example:**
```python
# src/modes/myNewMode.py
from ..skills import someSkill

class MyNewMode:
    ...
```
And in `agent.py`:
```python
from .modes.myNewMode import MyNewMode
modes.register(MyNewMode())
```

---

### Add or Update Diary Enrichment Feature
**Trigger:** When you want to add, fix, or enhance diary enrichment capabilities, especially around annotation, validation, or Telegram integration.  
**Command:** `/diary-enrichment`

1. Edit or create `scripts/diary_enrichment/__main__.py` for core logic.
2. Edit or create `scripts/diary_enrichment/validate.py` for validation logic.
3. Update `.gitignore` if new config or backup files are generated.
4. Refactor or move files as the feature evolves (e.g., renaming `diary_enchiment` → `diary_enrichment`, moving to `src/modes/`).
5. Update `src/modes/diary_enrichment/*` files as the feature is ported/refactored.

**Example:**
```python
# scripts/diary_enrichment/__main__.py
def enrich_diary_entry(entry):
    # LLM-driven annotation logic
    ...
```

---

### Add or Update Tool or API in Memory Providers
**Trigger:** When you want to add a new tool (e.g., recall, replace), update an API, or fix a bug in memory provider logic.  
**Command:** `/new-tool`

1. Edit or create files in `src/memory/providers/*` (e.g., `fact`, `personality`, `semantic`, `summary`, `tool`).
2. If adding a new tool, update the tool description and logic.
3. If needed, add or update corresponding test files in `tests/`.
4. Sometimes update `agent.py` if tool registration or agent logic is impacted.

**Example:**
```python
# src/memory/providers/tool.py
def recall_tool(memory, query):
    # Logic for recall
    ...
```
And in `tests/test_personality.py`:
```python
def test_recall_tool():
    ...
```

---

### Update Transport or Telegram Integration
**Trigger:** When you want to add or fix Telegram UI features, retry logic, or transport processing.  
**Command:** `/update-transport`

1. Edit `src/transport/telegram.py` for Telegram-specific features or fixes.
2. Edit `src/transport/base.py` or `src/transport/cli.py` for shared transport logic.
3. Update `main.py` if transport registration or CLI handling changes.
4. Update or add related files (e.g., send processing logic, context_used tool).

**Example:**
```python
# src/transport/telegram.py
def register_command(bot, command, handler):
    ...
```

---

### Project Configuration or Rule Migration
**Trigger:** When you want to migrate, split, or update config/rule files for the project or for external tool compatibility.  
**Command:** `/migrate-config`

1. Edit or move files in `.claude/` or `.cursor/` directories (rules, settings).
2. Update `.gitignore` for new patterns or venv changes.
3. Remove or migrate old config files (e.g., `pyrightconfig.json`, `start.bat`).
4. Document changes in `README.md` or `docs/`.

**Example:**
```json
// .claude/settings.json
{
  "memoryLimit": 1024,
  "enableTelegram": true
}
```

## Testing Patterns

- **Framework:** Unknown (not explicitly detected).
- **File Pattern:** Test files are named with the pattern `*.test.ts` (TypeScript), but Python test files like `tests/test_personality.py` and `tests/test_log_compressor.py` are also present.
- **Typical Test Example:**
  ```python
  # tests/test_personality.py
  def test_personality_update():
      ...
  ```

## Commands

| Command            | Purpose                                                        |
|--------------------|----------------------------------------------------------------|
| /new-mode          | Add or refactor an agent mode or skill                         |
| /diary-enrichment  | Add or update diary enrichment features                        |
| /new-tool          | Add or update a tool or API in memory providers                |
| /update-transport  | Update or fix transport/Telegram integration                   |
| /migrate-config    | Migrate or update project configuration or rule files          |
```
