# slonagent

Telegram-агент на базе Gemini с расширяемой долгосрочной памятью и скиллами.

## Возможности

- Общение через Telegram или CLI-режим
- Долгосрочная память: блоки, архив (RAG), история диалогов
- Автоматическая консолидация памяти в фоне (sleeptime agent)
- Выполнение кода и команд через `SandboxSkill`
- Управление конфигом агента через `ConfigSkill`
- Написание и загрузка новых скиллов через `SkillWriterSkill`
- Показывает процесс мышления модели (thinking mode)

## Структура

```
slonagent/
  agent.py            — ядро: Agent, Skill, @tool декоратор
  main.py             — точка входа
  memory.py           — скользящее окно контекста + провайдеры памяти
  src/
    memory/
      letta.py        — LettaProvider: блоки + архив (LanceDB) + история
      simplemem.py    — SimpleMemProvider: RAG на SimpleMem
      file.py         — FileProvider: саммари + история в .md файлах
      personality.py  — PersonalityProvider: субличности из .md файлов
      base.py         — BaseProvider: базовый класс с консолидацией
    skills/
      config.py       — ConfigSkill: управление .config.json
      sandbox.py      — SandboxSkill: выполнение кода
      skill_writer.py — SkillWriterSkill: написание новых скиллов
    transport/
      telegram.py     — Telegram-транспорт (aiogram)
      cli.py          — CLI-режим
  memory/             — данные памяти (в .gitignore)
    letta/            — блоки .md, archival.lancedb, history.jsonl
    simplemem/        — LanceDB для SimpleMem
    personalities/    — субличности .md
    CONTEXT.json      — скользящее окно диалога
```

## Установка

```bash
git clone https://github.com/boomyjee/slonagent.git
cd slonagent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Конфигурация

Скопируй `.config.sample.json` в `.config.json` и заполни:

```json
{
  "env": {
    "GEMINI_API_KEY": "...",
    "HTTPS_PROXY": "http://user:pass@host:port"
  },
  "telegram_transport": {
    "bot_token": "...",
    "allowed_user_ids": [123456789]
  },
  "agent": {
    "model_name": "gemini-2.0-flash",
    "api_key": "$GEMINI_API_KEY",
    "memory_providers": [
      {"__class__": "src.memory.letta.LettaProvider"}
    ],
    "skills": [
      {"__class__": "src.skills.config.ConfigSkill"},
      {"__class__": "src.skills.sandbox.SandboxSkill"},
      {"__class__": "src.skills.skill_writer.SkillWriterSkill"}
    ]
  }
}
```

## Запуск

```bash
# Telegram
start.bat

# CLI (без Telegram)
start.bat --cli
```

## Память

Провайдеры памяти подключаются в `memory_providers` в конфиге. Можно использовать несколько одновременно.

| Провайдер | Что делает |
|---|---|
| `LettaProvider` | Блоки памяти (всегда в контексте) + архив с семантическим поиском (LanceDB + Gemini embeddings) + полная история диалогов |
| `SimpleMemProvider` | RAG на базе SimpleMem (SQLite + LanceDB) |
| `FileProvider` | Саммари + история в Markdown-файлах |
| `PersonalityProvider` | Субличности — именованные блоки знаний, управляемые агентом |

### LettaProvider

Реализует все три слоя памяти из [Letta](https://github.com/letta-ai/letta):

- **Core Memory** — именованные блоки (`human.md`, `persona.md`, ...) всегда в системном промпте
- **Archival Memory** — долгосрочный архив фактов с семантическим поиском
- **Recall Memory** — полная история диалогов с поиском по тексту и дате

Блоки хранятся в `memory/letta/*.md` (совместимый с Letta формат — Markdown + YAML frontmatter).  
После каждых ~2000 токенов запускается фоновый sleeptime-агент, который сам решает что и куда сохранить.
