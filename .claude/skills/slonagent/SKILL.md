```markdown
# slonagent Development Patterns

> Auto-generated skill from repository analysis

## Overview

This skill teaches you the core development patterns, coding conventions, and collaborative workflows used in the `slonagent` Python codebase. The repository is structured around modular agent logic, memory/fact providers, and transport integrations, with a focus on clear commit practices and iterative feature development. Whether you're contributing new features, fixing bugs, or updating documentation, this guide will help you follow the established practices for efficient and maintainable development.

## Coding Conventions

- **File Naming:**  
  Use `camelCase` for file names.  
  _Example:_  
  ```
  myModule.py
  agentCoreLogic.py
  ```

- **Import Style:**  
  Use **relative imports** within modules.  
  _Example:_  
  ```python
  from .utils import parse_message
  from ..memory.providers.fact import recall
  ```

- **Export Style:**  
  Use **named exports** (explicitly listing what is exported).  
  _Example:_  
  ```python
  __all__ = ["Agent", "run_agent"]
  ```

- **Commit Messages:**  
  Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):  
  - Prefixes: `feat`, `fix`, `refactor`, `docs`, `chore`
  - Scope in parentheses when relevant: e.g., `feat(agent): add new tool execution`
  - Average commit message length: ~67 characters

## Workflows

### Diary Enrichment Feature or Fix
**Trigger:** When you want to add, fix, or improve diary enrichment logic or UI.  
**Command:** `/update-diary-enrichment`

1. Edit `scripts/diary_enchiment/__main__.py` to implement the feature or fix.
2. Optionally, update `scripts/diary_enchiment/validate.py` for validation changes.
3. Commit with a message prefixed by `feat(diary)`, `fix(diary)`, `ui(diary)`, or `prompt(diary)`.

_Example commit:_
```
feat(diary): add support for multi-entry enrichment
```

---

### Agent.py Feature or Fix
**Trigger:** When you want to change core agent behavior, tool execution, or add new agent features.  
**Command:** `/update-agent`

1. Edit `agent.py` to implement the new feature or fix.
2. Optionally, update related files in `src/skills/` or `src/transport/` for supporting changes.
3. Commit with a message prefixed by `feat(agent)`, `fix`, or `refactor(agent)`.

_Example commit:_
```
feat(agent): add fallback tool execution strategy
```

---

### Transport Telegram Feature or Fix
**Trigger:** When you want to improve Telegram bot integration or fix related issues.  
**Command:** `/update-telegram-transport`

1. Edit `src/transport/telegram.py` to add the feature or fix.
2. Optionally, update `agent.py` or related transport files for integration.
3. Commit with a message prefixed by `fix(telegram)`, `feat`, or similar.

_Example commit:_
```
fix(telegram): handle sticker messages gracefully
```

---

### Fact Provider Feature or Fix
**Trigger:** When you want to improve memory/fact extraction, recall, or retention.  
**Command:** `/update-fact-provider`

1. Edit files in `src/memory/providers/fact/` (e.g., `__init__.py`, `retain.py`, `recall.py`) to implement the feature or fix.
2. Optionally, update scripts like `scripts/retain_diary.py` or `scripts/migrate_fact_sources.py` for migrations or new flows.
3. Commit with a message prefixed by `feat(retain)`, `feat(recall)`, `fix`, etc.

_Example commit:_
```
feat(recall): improve fuzzy matching for fact retrieval
```

---

### Readme or Ideas Docs Update
**Trigger:** When you want to document new features, update architecture explanations, or log new ideas.  
**Command:** `/update-docs`

1. Edit `README.md` or `IDEAS.md` with new documentation or ideas.
2. Commit with a message prefixed by `docs` or `docs(ideas)`.

_Example commit:_
```
docs: update architecture diagram for new transport layer
```

---

### LLM Provider Migration or Refactor
**Trigger:** When you want to switch LLM providers or refactor all LLM-related code.  
**Command:** `/migrate-llm-provider`

1. Edit `requirements.txt` and `.config.sample.json` for the new provider.
2. Update all provider usages in `src/memory/providers/`, `src/skills/`, `src/transport/`, and `tests/`.
3. Commit with a message indicating migration or refactor.

_Example commit:_
```
refactor: migrate from Gemini to OpenAI-compatible LLM provider
```

---

## Testing Patterns

- **Test File Pattern:**  
  Test files use the `*.test.ts` naming convention.  
  _Example:_  
  ```
  agent.test.ts
  memoryProvider.test.ts
  ```
- **Framework:**  
  The specific testing framework is unknown, but the `.test.ts` pattern suggests a TypeScript-based test runner (e.g., Jest, Vitest).

## Commands

| Command                  | Purpose                                                        |
|--------------------------|----------------------------------------------------------------|
| /update-diary-enrichment | Start a diary enrichment feature or fix workflow               |
| /update-agent            | Start a core agent logic feature or fix workflow               |
| /update-telegram-transport | Start a Telegram transport feature or fix workflow           |
| /update-fact-provider    | Start a fact provider feature or fix workflow                  |
| /update-docs             | Update documentation (README or IDEAS)                         |
| /migrate-llm-provider    | Start an LLM provider migration or refactor workflow           |
```
