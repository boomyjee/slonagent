# slonagent

Telegram-агент на базе Gemini с долгосрочной памятью, выполнением кода и расширяемыми скиллами.

## Что умеет

- Общается через Telegram, поддерживает историю диалога
- Выполняет код и команды в Docker-контейнере через `ExecSkill`
- Помнит всё между сессиями — через `SimplememSkill` (SimpleMem-Cross: SQLite + LanceDB)
- Отправляет файлы, фото, документы обратно в чат
- Показывает процесс "мышления" модели (thinking mode)
- Поддерживает текстовые скиллы через [ClawHub](https://github.com/clawhub)
- Хранит JSON-конфиг агента, управляемый командами `/config`

## Структура

```
slonagent/
  agent.py          — ядро: Agent, Skill, @tool декоратор
  main.py           — точка входа
  transport.py      — Telegram-транспорт (aiogram)
  memory.py         — старый скилл памяти (резервный)
  simplemem_skill.py— долгосрочная память на SimpleMem-Cross
  exec.py           — выполнение кода в Docker
  config.py         — управление JSON-конфигом агента
  clawhub.py        — загрузка текстовых скиллов из workspace/skills/
  skill_manager.py  — управление скиллами агента
  lib/
    SimpleMem/      — клон https://github.com/aiming-lab/SimpleMem
```

## Установка

### 1. Клонировать репо

```bash
git clone <repo-url>
cd slonagent
```

### 2. Создать виртуальное окружение

```bash
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # Linux/Mac
```

### 3. Установить зависимости проекта

```bash
pip install -r requirements.txt
```

### 4. Установить зависимости SimpleMem

```bash
pip install -r lib/SimpleMem/requirements.txt
pip install fastapi
```

### 5. Настроить конфиг SimpleMem

```bash
cp lib/SimpleMem/config.py.example lib/SimpleMem/config.py
```

`config.py` уже настроен на Gemini — значения берутся из переменных окружения автоматически, ничего менять не нужно.

### 6. Создать `.env`

```env
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash
GEMINI_MEMORY_MODEL=gemini-2.0-flash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=123456789
MEMORY_BACKEND=simplemem   # simplemem (по умолчанию) или legacy

# Опционально, если нужен прокси
HTTP_PROXY=http://user:pass@host:port
HTTPS_PROXY=http://user:pass@host:port
```

### 7. Запустить

```bash
python main.py
# или
start.bat
```

При первом запуске `SimplememSkill` скачает embedding-модель `all-MiniLM-L6-v2` (~90 MB) с HuggingFace.

## Память

Долгосрочная память хранится в `memory/simplemem/`:
- `cross_memory.db` — SQLite: сессии, события, наблюдения
- `lancedb/` — векторное хранилище для семантического поиска

Агент автоматически сохраняет каждый диалог и подтягивает релевантный контекст из прошлых сессий в системный промпт.

## Команды в Telegram

| Команда | Описание |
|---|---|
| `/config` | Показать весь конфиг |
| `/config read <key>` | Показать значение ключа |
| `/config write <key> <value>` | Установить значение |
| `/config write <key>` | Удалить ключ |
