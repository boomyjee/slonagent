# slonagent

Telegram-агент на базе Gemini с расширяемой долгосрочной памятью и скиллами.

## Возможности

- Общение через Telegram или CLI-режим
- Долгосрочная память: несколько независимых провайдеров, подключаемых одновременно
- Сжатие истории диалогов: несколько стратегий компрессии
- Временны́е метки в каждом сообщении — агент всегда знает когда что происходило
- Пересланные сообщения в Telegram: агент видит от кого переслано
- Выполнение кода и команд через `SandboxSkill`
- Генерация изображений через `NanoBananaSkill` (поддержка входных изображений)
- Поиск в интернете через `BraveSearchSkill`
- Планировщик задач `CronSkill`: одноразовые и повторяющиеся (hourly/daily/weekly)
- Написание и загрузка новых скиллов прямо в диалоге через `SkillWriterSkill`
- Управление конфигом агента через `ConfigSkill`
- Показывает процесс мышления модели (thinking mode)

## Установка

Требуется **Python 3.12**. Версии 3.13+ пока не совместимы с некоторыми бинарными зависимостями (`pandas`, `lancedb`).

Требуется **[Podman](https://podman.io/)** — используется в `SandboxSkill` для изолированного выполнения кода и команд. Установи и убедись, что `podman` доступен в PATH.

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
    "GEMINI_API_KEY": "YOUR_API_KEY",
    "HTTPS_PROXY": "http://user:pass@host:port"
  },
  "telegram_transport": {
    "bot_token": "YOUR_BOT_TOKEN",
    "allowed_user_ids": [123456789],
    "verbose": true
  },
  "agent": {
    "model_name": "gemini-3-flash-preview",
    "transcription_model_name": "gemini-3-flash-preview",
    "api_key": "$GEMINI_API_KEY",
    "include_thoughts": true,
    "memory_compressor": {
      "__class__": "src.memory.compressors.log.LogCompressor",
      "api_key": "$GEMINI_API_KEY",
      "model_name": "gemini-3-flash-preview",
      "recent_tokens": 8000,
      "compress_after_tokens": 30000,
      "reflect_after_tokens": 40000
    },
    "memory_providers": [
      {
        "__class__": "src.memory.providers.fact.FactProvider",
        "model_name": "gemini-2.5-flash",
        "api_key": "$GEMINI_API_KEY",
        "auto_recall": false,
        "auto_consolidate": true,
        "embedding_model": {
          "provider": "google",
          "model": "models/gemini-embedding-001",
          "api_key": "$GEMINI_API_KEY"
        }
      },
      {
        "__class__": "src.memory.providers.personality.PersonalityProvider"
      },
      {
        "__class__": "src.memory.providers.tool.ToolProvider",
        "model_name": "gemini-3-flash-preview",
        "api_key": "$GEMINI_API_KEY"
      }
    ],
    "skills": [
      {"__class__": "src.skills.config.ConfigSkill"},
      {"__class__": "src.skills.sandbox.SandboxSkill"},
      {"__class__": "src.skills.skill_writer.SkillWriterSkill"},
      {"__class__": "src.skills.cron.CronSkill"},
      {"__class__": "src.skills.nano_banana.NanoBananaSkill", "api_key": "$GEMINI_API_KEY"},
      {"__class__": "src.skills.brave_search.BraveSearchSkill", "api_key": "YOUR_BRAVE_API_KEY"}
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

## Компрессоры истории

Компрессор задаётся в `memory_compressor` в конфиге. Отвечает за то, что происходит с историей диалога когда она вырастает слишком длинной.

| Компрессор | Что делает |
|---|---|
| `WindowCompressor` | Простое скользящее окно — отбрасывает старые сообщения |
| `SmartCompressor` | Порт [ReMe](https://github.com/modelscope/ReMe) (Alibaba) — компактизация, сжатие и выгрузка сообщений через LLM |
| `LogCompressor` | Порт Mastra Observational Memory — Observer превращает историю в наблюдения, Reflector периодически их уплотняет |

### LogCompressor

Реализация [Mastra Observational Memory](https://mastra.ai/docs/memory/overview) 1:1 с теми же промптами.

Параметры:
- `compress_after_tokens` — сколько токенов должно накопиться в истории, прежде чем Observer запустится (default: 30000)
- `recent_tokens` — сколько токенов свежей истории оставлять несжатыми (default: 6000)
- `reflect_after_tokens` — при каком объёме наблюдений запускается Reflector (default: 40000)

## Провайдеры памяти

Провайдеры подключаются в `memory_providers` в конфиге. Можно использовать несколько одновременно — каждый отвечает за свой слой памяти.

| Провайдер | Что делает |
|---|---|
| `FactProvider` | Граф фактов с семантическим поиском (SQLite + LanceDB). Реализация Hindsight 1:1 |
| `SummaryProvider` | Живой документ `MEMORY.md` + хронологический архив `HISTORY.md` |
| `PersonalityProvider` | Субличности — именованные Markdown-блоки знаний, управляемые агентом |
| `SemanticProvider` | RAG на базе векторного поиска (LanceDB + Qwen3-Embedding) |
| `ToolProvider` | Статистика и обогащённые описания инструментов на основе истории их использования |
| `HindsightProvider` | Интеграция с внешним Hindsight-сервером (устаревший, используй `FactProvider`) |
| `SimpleMemProvider` | Обёртка над библиотекой simplemem (устаревший, используй `SemanticProvider`) |

### FactProvider

Локальная реализация [Hindsight](https://github.com/vectorize-io/hindsight) без внешних серверов.

Пайплайн при каждой консолидации:
1. **retain** — разбивает диалог на чанки, LLM извлекает факты, сущности дедуплицируются через EntityResolver, факты сохраняются в SQLite + LanceDB
2. **create_observations** — кластеризует факты по сущностям, LLM синтезирует observations, сохраняются в БД
3. **recall** — при запросе ищет релевантные факты через RRF (векторный + полнотекстовый поиск) с cross-encoder reranking

Инструменты агента: `fact_recall`, `fact_get_document`, `fact_reflect`.

### SummaryProvider

Идея из [nanobot](https://github.com/HKUDS/nanobot). Два файла в `memory/summary/`:
- `MEMORY.md` — актуальный документ с ключевыми фактами, LLM обновляет при каждой консолидации
- `HISTORY.md` — хронологический архив выжимок диалогов (append-only)

Инструменты агента: `read_history`.

### PersonalityProvider

Субличности хранятся в `memory/personalities/`. Каждая — Markdown-файл с описанием и содержимым.  
Активные субличности всегда в системном промпте. Агент управляет ими через инструменты `load_personality`, `update_personality`, `create_personality`.

### SemanticProvider

Реимплементация ядра [SimpleMem](https://github.com/Qwen-LM/SimpleMem) без внешней зависимости.

Пайплайн при записи: LLM извлекает структурированные `MemoryEntry` (текст, ключевые слова, временна́я метка, локация, персоны, сущности) → Qwen3-Embedding → LanceDB.  
При поиске: запрос → Qwen3-Embedding → top-10 по векторной близости.

Данные в `memory/semantic/lancedb/`, дамп последней консолидации — `last_entries.json`.  
Инструмент агента: `search_memory`.

### ToolProvider

Идея из [ReMe](https://github.com/modelscope/ReMe) (Tool Memory). Накапливает статистику по каждому инструменту (число вызовов, success rate, среднее время, средний расход токенов) и генерирует обогащённое описание через LLM на основе реальных примеров использования из диалога.

Описание автоматически подмешивается в объявление инструмента перед каждым вызовом LLM — модель видит не только сигнатуру, но и рекомендации из накопленного опыта: когда использовать, что работает, что нет.

Данные в `memory/tool/tool_memory.json`.

## Скиллы

| Скилл | Что делает |
|---|---|
| `ConfigSkill` | Чтение и запись конфига агента через команду `/config` |
| `SandboxSkill` | Выполнение кода и shell-команд в контролируемой среде |
| `SkillWriterSkill` | Написание и горячая загрузка новых скиллов прямо в диалоге |
| `CronSkill` | Планировщик: агент ставит себе задачи на время, поддержка повторений |
| `NanoBananaSkill` | Генерация изображений через Gemini Imagen (поддержка входных изображений) |
| `BraveSearchSkill` | Поиск в интернете через Brave Search API |

### CronSkill

Агент может планировать задачи через инструменты `schedule_task`, `cancel_task`, `list_tasks`.  
Список задач также доступен через команду `/cron`.

Задачи хранятся в `memory/CRON.json` (JSONL). Поддерживаемые режимы повторения: `once`, `hourly`, `daily`, `weekly`.

### NanoBananaSkill

Генерация изображений через официальный API Google Gemini. Поддерживает несколько моделей:

| Модель | Описание |
|---|---|
| `nano_banana` | Gemini 2.5 Flash Image — быстро |
| `nano_banana_2` | Gemini 3.1 Flash Image — качественно |
| `nano_banana_pro` | Gemini 3 Pro Image — текст и сложная логика |

Параметр `images` принимает список путей к входным изображениям — для редактирования или стилизации.
