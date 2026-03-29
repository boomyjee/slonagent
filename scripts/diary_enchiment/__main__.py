"""diary_enchiment — интерактивный луп обогащения дневника inline ID-аннотациями.

Запуск: python scripts/diary_enchiment
Конфиг: scripts/diary_enchiment/config.json
Данные: scripts/diary_enchiment/data/
"""
import asyncio
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
from openai import AsyncOpenAI

# ── Paths ──────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CONFIG_PATH   = ROOT / "config.json"
STATE_PATH    = DATA / "state.json"
GLOSSARY_PATH = DATA / "glossary.md"
SOUL_PATH     = DATA / "SOUL.md"

DIARY_YEARS = list(range(2014, 2025))
PHOTOS_DIR  = Path("h:/fotki_dnevnik_new")
COLLAGE_MAX = 9
COLLAGE_SIZE = 1280

# ── Protocol ───────────────────────────────────────────────────────────────────

PROTOCOL = """ПРОТОКОЛ ОБОГАЩЕНИЯ ДНЕВНИКА (INLINE ID)

Цель: вставить аннотации [ID:Category:Name] непосредственно в текст дневника после
слов/фраз, к которым они относятся, сохраняя оригинальный текст нетронутым.

ФОРМАТ:
  Оригинал:    "Вася пошёл кататься на мотоцикле"
  Обогащённый: "Вася[ID:Pers:Vasya] пошёл кататься на мотоцикле[ID:Moto:CPI]"

ЖЁСТКИЕ ПРАВИЛА:
1. ID вставляется СРАЗУ ПОСЛЕ слова/фразы — без пробела между словом и [
2. Если удалить все [ID:...] из аннотированного текста, должен получиться
   ТОЧНО ОРИГИНАЛЬНЫЙ ТЕКСТ (до символа)
3. Не изменяй ни одного символа оригинала: никаких пробелов, знаков, переносов
4. КРИТЕРИЙ АННОТИРОВАНИЯ — "незнакомый читатель":
   Аннотируй то, что незнакомый читатель не поймёт без подсказки.
   Аннотировать НУЖНО:
   - личные знакомые автора, даже написанные полным именем (Арбузов, Федор)
   - личные проекты автора (BeeJee, LpCandy, Взаперти)
   - аббревиатуры и сокращения ЛЮБЫХ объектов (DS2, ЖЖ, ЛЖ, дд)
   - прозвища и клички (Буз, Тео, Наса, Ко)
   - локальные места, которые не найти в общем поиске (Беседница, Тануки СПб)
   Аннотировать НЕ НУЖНО:
   - публичных людей, написанных полным именем (Пелевин, Путин, Толстой)
   - известные произведения, написанные полным названием (Dark Souls 2, The Wire)
   - общеизвестные места, написанные полным именем (Минск, Санкт-Петербург, Египет)
   Правило: если сущность написана полностью И является публичной — не аннотируй.
   Если сокращена, или это личный знакомый/проект — аннотируй.
5. ЛОКАЦИЯ СТАВИТСЯ ПОСЛЕ ДАТЫ: локацию автора вставляй прямо в строке заголовка
   дня, сразу после его окончания (после дня недели / времени), без пробела.
   Пример заголовка: "2014.04.23 - ср" → "2014.04.23 - ср[ID:Loc:Kyiv]"
   Аннотируй только одну локацию — где автор ФИЗИЧЕСКИ находится в этот день.
   Другие упомянутые места как локации не аннотируй.
   [ID:Loc:...] ОБЯЗАТЕЛЕН для каждого дня — день без локации не пройдёт валидацию.
5а. ВСЕ ДНИ НЕДЕЛИ ОБЯЗАТЕЛЬНЫ: enrich_diary должен содержать каждый день из переданного
   списка. Нельзя пропустить ни один день — даже если там нечего аннотировать,
   кроме локации. Пропущенный день — ошибка валидации.
6. Если сущность есть в глоссарии — используй её ID
7. Если сущности нет в глоссарии — СНАЧАЛА вызови glossary_add, потом вставь ID
8. Если что-то неясно (незнакомое имя, непонятное сокращение) — вызови ask_user
   ДО enrich_diary
9. Если описание существующей записи в глоссарии неточное, неполное или устарело —
   вызови glossary_update, чтобы уточнить/дополнить описание. Например: добавить
   фамилию, уточнить род занятий, исправить ошибку.

ПОРЯДОК РАБОТЫ:
1. Прочитай все дни недели, определи где автор находится каждый день
2. Выдели неочевидные сущности (имена, проекты, сленг, аббревиатуры)
3. Добавь новые сущности (glossary_add) и обнови неточные описания (glossary_update)
4. При неясностях — задай вопросы (ask_user)
5. Вызови enrich_diary: локация — первый ID, сразу после строки даты"""

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"Нет config.json. Создай {CONFIG_PATH} по примеру config.json.example")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

# ── State ──────────────────────────────────────────────────────────────────────

@dataclass
class State:
    year: int
    week_idx: int

def load_state() -> State:
    if STATE_PATH.exists():
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return State(d["year"], d["week_idx"])
    return State(year=2014, week_idx=0)

def save_state(state: State):
    STATE_PATH.write_text(
        json.dumps({"year": state.year, "week_idx": state.week_idx},
                   indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

# ── Diary parsing ──────────────────────────────────────────────────────────────

_DAY_HEADER = re.compile(r'^(\d{4}\.\d{2}\.\d{2})(?:[ \t]+-[ \t]+\S+)?', re.MULTILINE)

@dataclass
class DiaryDay:
    date_str: str
    date: date
    text: str  # full raw block: header line + body, rstripped

def parse_diary(year: int) -> list[DiaryDay]:
    path = DATA / f"diary_{year}.txt"
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    matches = list(_DAY_HEADER.finditer(content))
    days = []
    for i, m in enumerate(matches):
        date_str = m.group(1)
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        raw = content[m.start():body_end].rstrip()
        try:
            d = date.fromisoformat(date_str.replace(".", "-"))
        except ValueError:
            continue
        days.append(DiaryDay(date_str=date_str, date=d, text=raw))
    return days

def group_into_weeks(days: list[DiaryDay]) -> list[list[DiaryDay]]:
    """Группируем дни по ISO-неделям (пн–вс)."""
    if not days:
        return []
    weeks: list[list[DiaryDay]] = []
    current: list[DiaryDay] = []
    current_key: tuple | None = None
    for day in days:
        key = day.date.isocalendar()[:2]  # (iso_year, iso_week)
        if key != current_key:
            if current:
                weeks.append(current)
            current = [day]
            current_key = key
        else:
            current.append(day)
    if current:
        weeks.append(current)
    return weeks

# ── Glossary ───────────────────────────────────────────────────────────────────

_ENTRY_RE = re.compile(r'^- ID:([^:\s]+:[^:\s]+):\s*(.*)', re.MULTILINE)

def _read_glossary_parts() -> tuple[str, str]:
    """Возвращает (description, body) — до и после '---'."""
    if not GLOSSARY_PATH.exists():
        return "", ""
    text = GLOSSARY_PATH.read_text(encoding="utf-8")
    parts = text.split("---", 1)
    desc = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""
    return desc, body

def read_glossary_body() -> str:
    _, body = _read_glossary_parts()
    return body

def read_glossary_dict() -> dict[str, str]:
    _, body = _read_glossary_parts()
    return {k: v.strip() for k, v in _ENTRY_RE.findall(body)}

def _write_glossary(desc: str, body: str):
    GLOSSARY_PATH.write_text(f"{desc}\n---\n{body}\n", encoding="utf-8")

def glossary_add(id_key: str, description: str):
    desc, body = _read_glossary_parts()
    body = body.rstrip() + f"\n- ID:{id_key}: {description}"
    _write_glossary(desc, body)

def glossary_update(id_key: str, new_description: str):
    desc, body = _read_glossary_parts()
    new_body, n = re.subn(
        rf'^(- ID:{re.escape(id_key)}:).*',
        rf'\1 {new_description}',
        body,
        flags=re.MULTILINE
    )
    if n == 0:
        glossary_add(id_key, new_description)
    else:
        _write_glossary(desc, new_body)

# ── Enrichment I/O + Validation ────────────────────────────────────────────────

_ID_RE = re.compile(r'\[ID:[^\]]+\]')

def strip_ids(text: str) -> str:
    return _ID_RE.sub("", text)

def validate_enrichment(original: str, annotated: str) -> tuple[bool, str]:
    stripped = strip_ids(annotated)
    if stripped == original:
        return True, ""
    # Find first difference for a helpful error message
    a, b = original, stripped
    for i, (ca, cb) in enumerate(zip(a, b)):
        if ca != cb:
            ctx_a = repr(a[max(0, i-10):i+20])
            ctx_b = repr(b[max(0, i-10):i+20])
            return False, f"Первое расхождение на позиции {i}: оригинал={ctx_a}, stripped={ctx_b}"
    if len(a) != len(b):
        return False, f"Длина: оригинал={len(a)}, stripped={len(b)}"
    return False, "Неизвестное расхождение"

def load_enrichments(year: int) -> dict[str, str]:
    path = DATA / f"enrichment_{year}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

def save_enrichments(year: int, data: dict[str, str]):
    path = DATA / f"enrichment_{year}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

CENSORED_PATH = DATA / "censored.json"

def load_censored() -> dict[str, str]:
    if not CENSORED_PATH.exists():
        return {}
    text = CENSORED_PATH.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}

def save_censored(data: dict[str, str]):
    CENSORED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_old_hints(year: int) -> dict[str, list[str]]:
    path = DATA / f"old_enchiment_{year}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

# ── Compiled build ─────────────────────────────────────────────────────────────

def build_compiled(year: int):
    days = parse_diary(year)
    enrichments = load_enrichments(year)
    glossary = read_glossary_dict()

    out: list[str] = []
    for day in days:
        annotated = enrichments.get(day.date_str)
        out.append(annotated if annotated else day.text)

        if annotated:
            used: list[tuple[str, str]] = []
            seen: set[str] = set()
            for m in _ID_RE.finditer(annotated):
                id_full = m.group(0)[1:-1]  # убираем [ ]
                if id_full in seen:
                    continue
                seen.add(id_full)
                key = id_full[3:]  # убираем "ID:"
                desc = glossary.get(key, "?")
                used.append((id_full, desc))
            if used:
                out.append("")
                out.append("  Расшифровка:")
                for id_full, desc in used:
                    out.append(f"  • {id_full} — {desc}")

        out.append("")

    path = DATA / f"compiled_{year}.txt"
    path.write_text("\n".join(out), encoding="utf-8")
    return path

# ── Photos ─────────────────────────────────────────────────────────────────────

def find_day_photos(date_str: str) -> list[Path]:
    if not PHOTOS_DIR.exists():
        return []
    matches = list(PHOTOS_DIR.glob(f"{date_str}*"))
    if not matches or not matches[0].is_dir():
        return []
    return sorted(
        f for f in matches[0].iterdir()
        if f.is_file() and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
    )


def make_collage(paths: list[Path], size: int = COLLAGE_SIZE) -> io.BytesIO:
    """Собрать коллаж 3x3 (или меньше) из фото. Возвращает JPEG в BytesIO."""
    from PIL import Image, ImageOps

    n = len(paths)
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols
    cell = size // cols

    canvas = Image.new("RGB", (cols * cell, rows * cell), (0, 0, 0))
    for i, p in enumerate(paths):
        row, col = divmod(i, cols)
        with Image.open(str(p)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((cell, cell))
            x = col * cell + (cell - img.width) // 2
            y = row * cell + (cell - img.height) // 2
            canvas.paste(img, (x, y))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=60)
    buf.seek(0)
    return buf

# ── Telegram ───────────────────────────────────────────────────────────────────

class TgClient:
    _MAX_MSG = 4000

    def __init__(self, token: str, allowed_user_ids: list[int],
                 proxy: str | None = None):
        self._token = token
        self._allowed = set(allowed_user_ids)
        self._last_update_id: int | None = None
        # Куда отвечать — обновляется при каждом входящем сообщении
        self._reply_chat_id: int | None = None
        self._reply_thread_id: int | None = None
        transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
        self._http = httpx.AsyncClient(transport=transport, timeout=65.0)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def _chunks(self, text: str) -> list[str]:
        """Делит текст по строкам, не по символам — HTML-теги никогда не разрезаются."""
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in lines:
            needed = len(line) + (1 if current else 0)  # +1 для \n между строками
            if current and current_len + needed > self._MAX_MSG:
                chunks.append("\n".join(current))
                current = [line]
                current_len = len(line)
            else:
                current.append(line)
                current_len += needed
        if current:
            chunks.append("\n".join(current))
        return chunks or [""]

    async def notify(self, text: str):
        """Тихое лог-сообщение — отправляется без форматирования, не ждёт ответа."""
        print(f"[notify] {text}")
        if self._reply_chat_id is None:
            return
        try:
            payload: dict = {
                "chat_id": self._reply_chat_id,
                "text": f"<i>{text}</i>",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": True,
            }
            if self._reply_thread_id:
                payload["message_thread_id"] = self._reply_thread_id
            await self._http.post(self._url("sendMessage"), json=payload)
        except Exception as e:
            print(f"[tg] notify error: {e}")

    async def send(self, text: str):
        if self._reply_chat_id is None:
            print(f"[tg] no reply target yet, skipping: {text[:80]}")
            return
        for chunk in self._chunks(text):
            payload: dict = {
                "chat_id": self._reply_chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if self._reply_thread_id:
                payload["message_thread_id"] = self._reply_thread_id
            try:
                r = await self._http.post(self._url("sendMessage"), json=payload)
                rj = r.json()
                if not rj.get("ok"):
                    print(f"[tg] send FAILED: {r.status_code} {r.text[:300]}")
                else:
                    print(f"[tg] send ok, msg_id={rj['result']['message_id']}, len={len(chunk)}")
            except Exception as e:
                print(f"[tg] send error: {e}")

    async def send_collage(self, paths: list[Path]):
        if not paths or self._reply_chat_id is None:
            return
        bufs = []
        for i in range(0, len(paths), COLLAGE_MAX):
            buf = make_collage(paths[i:i + COLLAGE_MAX])
            print(f"[tg] collage part {i // COLLAGE_MAX + 1}: {min(COLLAGE_MAX, len(paths) - i)} photos, {buf.getbuffer().nbytes // 1024}KB")
            bufs.append(buf)
        try:
            media = []
            files = {}
            for idx, buf in enumerate(bufs):
                key = f"collage{idx}"
                media.append({"type": "photo", "media": f"attach://{key}"})
                files[key] = (f"{key}.jpg", buf, "image/jpeg")
            data: dict = {"chat_id": str(self._reply_chat_id), "media": json.dumps(media)}
            if self._reply_thread_id:
                data["message_thread_id"] = str(self._reply_thread_id)
            r = await self._http.post(
                self._url("sendMediaGroup"),
                data=data,
                files=files,
                timeout=120.0,
            )
            if not r.is_success:
                print(f"[tg] send_collage HTTP {r.status_code}: {r.text[:300]}")
        except Exception as e:
            print(f"[tg] send_collage error: {e}")
        finally:
            for buf in bufs:
                buf.close()

    async def drain_pending(self) -> None:
        """Сбрасывает все накопленные обновления — вызвать один раз при старте."""
        try:
            r = await self._http.post(
                self._url("getUpdates"),
                json={"timeout": 0, "allowed_updates": ["message"]},
                timeout=10.0,
            )
            r.raise_for_status()
            updates = r.json().get("result", [])
            if updates:
                self._last_update_id = updates[-1]["update_id"]
                print(f"[tg] сброшено {len(updates)} старых обновлений")
        except Exception as e:
            print(f"[tg] drain error: {e}")

    async def wait_for_message(self) -> str:
        """Polling до первого текстового сообщения от разрешённого пользователя."""
        while True:
            params: dict = {"timeout": 20, "allowed_updates": ["message"]}
            if self._last_update_id is not None:
                params["offset"] = self._last_update_id + 1
            try:
                r = await self._http.post(self._url("getUpdates"), json=params,
                                          timeout=25.0)
                r.raise_for_status()
                for upd in r.json().get("result", []):
                    self._last_update_id = upd["update_id"]
                    msg = upd.get("message", {})
                    from_id = msg.get("from", {}).get("id")
                    if from_id not in self._allowed:
                        continue
                    text = msg.get("text", "")
                    if not text:
                        continue
                    # Запомнить куда отвечать
                    self._reply_chat_id = msg["chat"]["id"]
                    self._reply_thread_id = msg.get("message_thread_id")
                    return text
            except Exception as e:
                print(f"[tg] polling error: {e}")
                await asyncio.sleep(3)

    async def close(self):
        await self._http.aclose()

# ── LLM tools definitions ──────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "enrich_diary",
            "description": (
                "Сохранить аннотированные тексты дней недели. "
                "ОБЯЗАТЕЛЬНО передавать ВСЕ дни недели — нельзя пропустить ни один, даже если там только локация. "
                "Для каждого дня передаётся ПОЛНЫЙ текст дня (включая заголовок) с вставленными "
                "[ID:Category:Name] маркерами. Если из аннотированного текста удалить все [ID:...], "
                "должен получиться ТОЧНЫЙ оригинальный текст. "
                "Первый ID каждого дня — обязательно [ID:Loc:...] в строке заголовка."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "array",
                        "description": "Массив дней с аннотированным текстом.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {
                                    "type": "string",
                                    "description": "Дата дня в формате YYYY.MM.DD, например 2014.05.12"
                                },
                                "text": {
                                    "type": "string",
                                    "description": "Полный аннотированный текст дня"
                                }
                            },
                            "required": ["date", "text"]
                        }
                    }
                },
                "required": ["days"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glossary_add",
            "description": "Добавить новую запись в глоссарий.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "ID в формате Category:Name, напр. Pers:Arbuzov, Loc:SPb"
                    },
                    "description": {
                        "type": "string",
                        "description": "Полное описание сущности"
                    }
                },
                "required": ["id", "description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glossary_update",
            "description": (
                "Обновить/дополнить описание существующей записи в глоссарии. "
                "Используй, когда из контекста дневника стало известно больше о сущности: "
                "полное имя, род занятий, уточнение локации и т.п."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Существующий ID, напр. Pers:Arbuzov"
                    },
                    "new_description": {
                        "type": "string",
                        "description": "Новое полное описание (заменяет старое целиком)"
                    }
                },
                "required": ["id", "new_description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Задать уточняющие вопросы пользователю через Telegram. "
                "Используй когда встречается неизвестное имя/сокращение/событие, "
                "которого нет в глоссарии и нельзя однозначно определить."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Список вопросов"
                    }
                },
                "required": ["questions"]
            }
        }
    }
]

# ── Approval helpers ───────────────────────────────────────────────────────────

def _is_approval(text: str) -> bool:
    """Короткое сообщение (< 3 символов) считается одобрением."""
    return len(text.strip()) < 3

def _split_reply(text: str) -> tuple[str, str]:
    """Разбивает ответ пользователя на фидбэк для LLM и заметки для SOUL.md.

    Строки с '+' в начале — идут и в фидбэк (без префикса), и в SOUL.md.
    Строки без '+' — только в фидбэк.
    Возвращает (feedback, soul_note).
    """
    feedback_lines, soul_lines = [], []
    for line in text.splitlines():
        if line.lstrip().startswith("+"):
            content = line.lstrip().lstrip("+").strip()
            soul_lines.append(content)
            feedback_lines.append(content)
        else:
            feedback_lines.append(line)
    return "\n".join(feedback_lines).strip(), "\n".join(soul_lines).strip()

def _append_soul(note: str) -> None:
    if not note:
        return
    with open(SOUL_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n\n---\n{note}\n")

_APPROVE_HINT = (
    "\n\n<i>Одобрить: отправь . или любой символ &lt;3 симв.</i>\n"
    "<i>Замечание: напиши текст. Строки с + в начале сохраняются в SOUL.md.</i>"
)

def _highlight_ids(text: str) -> str:
    parts = []
    last = 0
    for m in _ID_RE.finditer(text):
        parts.append(_html_escape(text[last:m.start()]))
        parts.append(f"<code>{_html_escape(m.group(0))}</code>")
        last = m.end()
    parts.append(_html_escape(text[last:]))
    return "".join(parts)

def _format_enrich_proposal(week: list[DiaryDay], proposed_days: dict[str, str]) -> str:
    glossary = read_glossary_dict()
    lines = ["📝 <b>Предлагаемое обогащение:</b>\n"]
    for day in week:
        annotated = proposed_days.get(day.date_str)
        if not annotated:
            continue
        text_body = annotated.removeprefix(day.date_str).lstrip()
        lines.append(f"📖 <b>{day.date_str}</b> {_highlight_ids(text_body)}")
        used: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in _ID_RE.finditer(annotated):
            id_full = m.group(0)[1:-1]
            if id_full in seen:
                continue
            seen.add(id_full)
            key = id_full[3:]
            used.append((id_full, glossary.get(key, "?")))
        if used:
            lines.append("")
            for id_full, desc in used:
                lines.append(f"<i>• {_html_escape(id_full)} — {_html_escape(desc)}</i>")
        lines.append("")
    return "\n".join(lines) + _APPROVE_HINT

def _format_glossary_add_proposal(id_key: str, description: str) -> str:
    return (
        f"📖 <b>Добавить в глоссарий:</b>\n"
        f"<code>ID:{_html_escape(id_key)}: {_html_escape(description)}</code>"
        + _APPROVE_HINT
    )

def _format_glossary_update_proposal(id_key: str, old_desc: str, new_desc: str) -> str:
    return (
        f"📖 <b>Изменить в глоссарии:</b>\n"
        f"<code>ID:{_html_escape(id_key)}</code>\n"
        f"Было: {_html_escape(old_desc)}\n"
        f"Станет: {_html_escape(new_desc)}"
        + _APPROVE_HINT
    )

def _feedback_note(feedback: list[str]) -> str:
    """Форматирует накопленные замечания для возврата LLM."""
    items = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(feedback))
    return f"ОТКЛОНЕНО пользователем.\n\nВсе замечания:\n{items}\n\nИсправь и повтори вызов."


def _get_prev_weeks_context(week: list[DiaryDay], year: int, n: int = 2) -> str:
    """Возвращает последние n сохранённых недель перед текущей в виде текста."""
    first_date = week[0].date_str
    enc = load_enrichments(year)
    # Берём все сохранённые дни строго до начала текущей недели, в хронологическом порядке
    prev_days = sorted(ds for ds in enc if ds < first_date)
    if not prev_days:
        return ""
    # Берём последние n*7 дней (грубо — n недель)
    tail = prev_days[-(n * 7):]
    lines = [f"--- ПОСЛЕДНИЕ {n} НЕДЕЛИ (сохранённый энричмент, для контекста) ---"]
    for ds in tail:
        lines.append(enc[ds])
    return "\n\n".join(lines)

_LLM_RETRY_DELAYS = [5, 15, 30, 60]

async def _llm_call(llm, tg: TgClient, **kwargs):
    for attempt, delay in enumerate(_LLM_RETRY_DELAYS, 1):
        try:
            return await llm.chat.completions.create(**kwargs)
        except Exception as e:
            code = getattr(e, 'status_code', None)
            if code in (429, 503) and attempt < len(_LLM_RETRY_DELAYS):
                await tg.notify(f"⚠️ LLM {code}: {e}. Retry {attempt}/{len(_LLM_RETRY_DELAYS)} через {delay}с...")
                await asyncio.sleep(delay)
            else:
                raise

# ── LLM enrichment loop ────────────────────────────────────────────────────────

async def run_enrichment_loop(
    week: list[DiaryDay],
    year: int,
    tg: TgClient,
    llm: AsyncOpenAI,
    model: str,
) -> dict[str, str]:
    """Запускает LLM-луп для одной недели. Возвращает {date: annotated_text}."""
    old_hints = load_old_hints(year)
    week_dates = {d.date_str for d in week}
    week_hints = {k: v for k, v in old_hints.items() if k in week_dates}

    date_from = week[0].date_str
    date_to   = week[-1].date_str

    # Дни помеченные как CENSORED исключаем из обработки LLM
    censored = load_censored()
    active_week = [d for d in week if d.date_str not in censored]
    week_text = "\n\n".join(d.text for d in active_week)

    def _build_system() -> str:
        soul = SOUL_PATH.read_text(encoding="utf-8").strip() if SOUL_PATH.exists() else ""
        parts = [PROTOCOL]
        if soul:
            parts.append(f"[SOUL]\n{soul}")
        parts.append(f"[GLOSSARY]\n{read_glossary_body()}")
        if week_hints:
            hints_lines = "\n".join(
                f"{ds}: {' | '.join(hints)}"
                for ds, hints in sorted(week_hints.items())
            )
            parts.append(
                "[ENRICHMENT HINTS — используй эти подсказки чтобы расставить ID, "
                "не задавая лишних вопросов]\n" + hints_lines
            )
        prev_ctx = _get_prev_weeks_context(week, year)
        if prev_ctx:
            parts.append(prev_ctx)
        return "\n\n".join(parts)

    week_date_list = ", ".join(d.date_str for d in active_week)
    messages: list[dict] = [
        {"role": "system", "content": _build_system()},
        {"role": "user", "content": (
            f"[DIARY WEEK {date_from} — {date_to}]\n"
            f"Дни недели (используй эти строки как ключи в enrich_diary): {week_date_list}\n\n"
            f"{week_text}"
        )}
    ]

    enrichments_week: dict[str, str] = {}
    # Накопленные замечания пользователя — сохраняются на всю неделю
    accumulated_feedback: list[str] = []

    actual_dates = [d.date_str for d in active_week]
    if len(active_week) < len(week):
        skipped = [d.date_str for d in week if d.date_str in censored]
        await tg.notify(f"📅 Даты в week: {actual_dates} | CENSORED (пропущены): {skipped}")
    else:
        await tg.notify(f"📅 Реальные даты в week: {actual_dates}")

    llm_turn = 0
    while True:
        messages[0]["content"] = _build_system()
        llm_turn += 1
        await tg.notify(f"🤔 LLM думает (итерация {llm_turn})...")
        resp = await _llm_call(llm, tg, model=model, messages=messages, tools=TOOLS, tool_choice="auto")
        if not resp.choices or resp.choices[0].message is None:
            reason = resp.choices[0].finish_reason if resp.choices else "no choices"
            await tg.notify(f"⚠️ LLM вернула пустой ответ (finish_reason={reason})")
            if "content_filter" in str(reason):
                fallback_cfg = json.loads((CONFIG_PATH.parent / "config_censored.json").read_text(encoding="utf-8"))
                await tg.notify(f"🚫 content_filter — переключаюсь на {fallback_cfg.get('model', '?')} для этой недели")
                fallback_http = httpx.AsyncClient(proxy=fallback_cfg.get("proxy"), timeout=120.0)
                fallback_llm = AsyncOpenAI(
                    api_key=fallback_cfg["api_key"],
                    base_url=fallback_cfg.get("api_base_url", "https://openrouter.ai/api/v1"),
                    http_client=fallback_http,
                )
                try:
                    return await run_enrichment_loop(week, year, tg, fallback_llm, fallback_cfg.get("model", model))
                finally:
                    await fallback_http.aclose()
            elif llm_turn >= 5:
                await tg.notify("❌ LLM 5 раз подряд вернула пустой ответ — прерываю неделю")
                return {}
            continue
        msg = resp.choices[0].message

        assistant_msg: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            tc_list = []
            for tc in msg.tool_calls:
                tc_dict = {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                # Gemini thinking models attach thought_signature to tool calls.
                # Must be echoed back in subsequent requests to avoid 400 error.
                tc_extra = getattr(tc, "model_extra", None) or {}
                sig = (tc_extra.get("extra_content", {})
                               .get("google", {})
                               .get("thought_signature"))
                if sig:
                    tc_dict["extra_content"] = {"google": {"thought_signature": sig}}
                tc_list.append(tc_dict)
            assistant_msg["tool_calls"] = tc_list
        messages.append(assistant_msg)

        if not msg.tool_calls:
            messages.append({"role": "user", "content": "Ты не вызвал enrich_diary. Обработай неделю и вызови enrich_diary со всеми днями."})
            continue

        tool_calls = list(msg.tool_calls)
        await tg.notify(f"🔧 Инструменты: {', '.join(tc.function.name for tc in tool_calls)}")

        tool_results: list[dict] = []
        enrich_approved = False

        _glossary_names = {"glossary_add", "glossary_update"}
        glossary_calls = [c for c in tool_calls if c.function.name in _glossary_names]
        other_calls = [c for c in tool_calls if c.function.name not in _glossary_names]
        glossary_rejected = False
        for call in glossary_calls:
            fn = call.function.name
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                args = {}

            if fn == "glossary_add":
                if args["id"].startswith("ID:"):
                    args["id"] = args["id"][3:]
                proposal = _format_glossary_add_proposal(args["id"], args["description"])
                await tg.send(proposal)
                answer = await tg.wait_for_message()
                if _is_approval(answer):
                    glossary_add(args["id"], args["description"])
                    result = f"Добавлен ID:{args['id']}"
                else:
                    feedback, soul_note = _split_reply(answer)
                    _append_soul(soul_note)
                    result = f"ОТКЛОНЕНО: {feedback}" if feedback else "ОТКЛОНЕНО пользователем."
                    glossary_rejected = True

            elif fn == "glossary_update":
                old_desc = read_glossary_dict().get(args["id"], "")
                proposal = _format_glossary_update_proposal(
                    args["id"], old_desc, args["new_description"]
                )
                await tg.send(proposal)
                answer = await tg.wait_for_message()
                if _is_approval(answer):
                    glossary_update(args["id"], args["new_description"])
                    result = f"Обновлён ID:{args['id']}"
                else:
                    feedback, soul_note = _split_reply(answer)
                    _append_soul(soul_note)
                    result = f"ОТКЛОНЕНО: {feedback}" if feedback else "ОТКЛОНЕНО пользователем."
                    glossary_rejected = True

            tool_results.append({"role": "tool", "tool_call_id": call.id, "content": result})

        for call in other_calls:
            fn = call.function.name
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                args = {}

            if fn == "enrich_diary":
                if glossary_rejected:
                    result = (
                        "enrich_diary отклонён: изменение глоссария было отклонено пользователем. "
                        "Исправь глоссарий и повтори вызов."
                    )
                    tool_results.append({"role": "tool", "tool_call_id": call.id, "content": result})
                    continue
                # LLM иногда оборачивает ключи в лишние кавычки или генерирует
                # html-anchor стиль (h2014_07_21) — нормализуем к YYYY.MM.DD
                days_list = args.get("days", [])
                if isinstance(days_list, dict):
                    days_list = [{"date": k, "text": v} for k, v in days_list.items()]
                bad_items = [i for i, item in enumerate(days_list) if not item.get("date") or not item.get("text")]
                if bad_items:
                    valid = ", ".join(d.date_str for d in active_week)
                    result = (
                        f"ОШИБКА ФОРМАТА: элементы {bad_items} не содержат date или text. "
                        f"Каждый элемент массива days должен быть {{\"date\": \"YYYY.MM.DD\", \"text\": \"...\"}}. "
                        f"Допустимые даты: {valid}"
                    )
                    tool_results.append({"role": "tool", "tool_call_id": call.id, "content": result})
                    continue
                days_arg = {item["date"]: item["text"] for item in days_list}
                await tg.notify(f"🔍 days_arg ключей: {len(days_arg)}, первые: {list(days_arg.keys())[:3]}")
                # Валидация
                errors: list[str] = []
                valid_days: dict[str, str] = {}
                print(f"[debug] начинаю валидацию {len(days_arg)} дней")
                for ds, annotated in days_arg.items():
                    print(f"[debug] валидирую {ds}, len(annotated)={len(annotated) if isinstance(annotated, str) else type(annotated)}")
                    orig = next((d.text for d in active_week if d.date_str == ds), None)
                    if orig is None:
                        valid = ", ".join(d.date_str for d in active_week)
                        errors.append(f"Ключ \"{ds}\" не найден. Допустимые ключи: {valid}")
                        continue
                    print(f"[debug] вызываю validate_enrichment для {ds}")
                    ok, err = validate_enrichment(orig, annotated)
                    print(f"[debug] validate_enrichment для {ds}: ok={ok}")
                    if not ok:
                        errors.append(f"{ds}: {err}")
                    else:
                        valid_days[ds] = annotated
                print(f"[debug] валидация завершена: errors={len(errors)}, valid={len(valid_days)}")

                glossary = read_glossary_dict()
                for ds, annotated in list(valid_days.items()):
                    unknown = []
                    for m in _ID_RE.finditer(annotated):
                        tag = m.group(0)             # [ID:Pers:Arbuzov]
                        glossary_key = tag[4:-1]     # Pers:Arbuzov
                        if glossary_key.startswith("Loc:"):
                            continue
                        if glossary_key not in glossary:
                            unknown.append(tag)
                    if unknown:
                        errors.append(f"{ds}: ID отсутствуют в глоссарии: {', '.join(unknown)}")
                        valid_days.pop(ds)

                # Проверяем что все active дни недели присутствуют (censored не считаем)
                active_dates = {d.date_str for d in active_week}
                submitted = set(days_arg.keys())
                missing = active_dates - submitted
                for ds in sorted(missing):
                    errors.append(f"{ds}: день недели пропущен — все дни обязательны")

                print(f"[debug] проверяю локации в {len(valid_days)} днях")
                # Проверяем что локация стоит в первой строке (строке заголовка)
                for ds, annotated in list(valid_days.items()):
                    first_line = annotated.split("\n")[0]
                    loc_in_header = bool(re.search(r'\[ID:Loc:', first_line))
                    first_id = next(iter(_ID_RE.finditer(annotated)), None)
                    if not loc_in_header:
                        errors.append(
                            f"{ds}: локация автора [ID:Loc:...] должна стоять в строке "
                            f"заголовка (после даты), а не в теле записи"
                        )
                        valid_days.pop(ds)
                    elif first_id and not first_id.group(0).startswith("[ID:Loc:"):
                        errors.append(
                            f"{ds}: первый ID должен быть локацией [ID:Loc:...], "
                            f"а не {first_id.group(0)}"
                        )
                        valid_days.pop(ds)
                print(f"[debug] локации ок, итог: errors={len(errors)}, valid={len(valid_days)}")

                if errors:
                    err_text = "\n".join(errors)
                    await tg.notify(f"⚠️ Валидация не прошла, отправляю ошибки LLM:\n{err_text}")
                    result = "ОШИБКИ ВАЛИДАЦИИ:\n" + err_text
                    tool_results.append({"role": "tool", "tool_call_id": call.id, "content": result})
                else:
                    print(f"[debug] формирую proposal")
                    proposal = _format_enrich_proposal(week, valid_days)
                    print(f"[debug] proposal len={len(proposal)}, отправляю в tg")
                    await tg.send(proposal)
                    print(f"[debug] proposal отправлен, жду ответа пользователя")
                    answer = await tg.wait_for_message()
                    if _is_approval(answer):
                        await tg.notify(f"💾 Сохраняю {len(valid_days)} дней: {', '.join(valid_days)}")
                        enrichments_week.update(valid_days)
                        all_enc = load_enrichments(year)
                        all_enc.update(enrichments_week)
                        save_enrichments(year, all_enc)
                        build_compiled(year)
                        enrich_approved = True
                        break
                    else:
                        feedback, soul_note = _split_reply(answer)
                        _append_soul(soul_note)
                        accumulated_feedback.append(feedback)
                        result = _feedback_note(accumulated_feedback)
                        tool_results.append({"role": "tool", "tool_call_id": call.id, "content": result})
                continue  # tool_results уже appended выше

            elif fn == "ask_user":
                questions = args.get("questions", [])
                q_text = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
                await tg.send(f"❓ <b>Вопросы по неделе {date_from}:</b>\n\n{q_text}")
                answer = await tg.wait_for_message()
                result, soul_note = _split_reply(answer)
                _append_soul(soul_note)
                if not result:
                    result = answer

            else:
                result = f"Неизвестный инструмент: {fn}"

            tool_results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result
            })

        if enrich_approved:
            break
        messages.extend(tool_results)

    return enrichments_week

# ── Week summary (для отправки в Telegram) ─────────────────────────────────────

def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def build_week_summary(week: list[DiaryDay], year: int) -> str:
    enrichments = load_enrichments(year)
    glossary = read_glossary_dict()
    date_from = week[0].date_str
    date_to   = week[-1].date_str

    lines = [f"📅 <b>Неделя: {date_from} — {date_to}</b>\n"]
    for day in week:
        annotated = enrichments.get(day.date_str, day.text)
        lines.append(f"<b>{day.date_str}</b>")
        lines.append(_html_escape(annotated))

        used: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in _ID_RE.finditer(annotated):
            id_full = m.group(0)[1:-1]
            if id_full in seen:
                continue
            seen.add(id_full)
            key = id_full[3:]
            used.append((id_full, glossary.get(key, "?")))

        if used:
            lines.append("")
            lines.append("  <i>Расшифровка:</i>")
            for id_full, desc in used:
                lines.append(f"  • {_html_escape(id_full)} — {_html_escape(desc)}")
        lines.append("")

    return "\n".join(lines)

# ── Glossary feedback loop (post-week corrections) ─────────────────────────────

_GLOSSARY_TOOLS = [t for t in TOOLS if t["function"]["name"] in ("glossary_add", "glossary_update", "ask_user")]

async def run_glossary_feedback_loop(
    user_comment: str,
    tg: "TgClient",
    llm: AsyncOpenAI,
    model: str,
) -> None:
    """Мини-петля для уточнений глоссария после завершения недели."""
    def _build_system() -> str:
        soul = SOUL_PATH.read_text(encoding="utf-8").strip() if SOUL_PATH.exists() else ""
        return "\n\n".join(filter(None, [
            PROTOCOL,
            soul and f"[SOUL]\n{soul}",
            f"[GLOSSARY]\n{read_glossary_body()}",
        ]))

    messages: list[dict] = [
        {"role": "system", "content": _build_system()},
        {"role": "user", "content": f"Пользователь хочет уточнить записи в словаре:\n{user_comment}"},
    ]
    accumulated_feedback: list[str] = []

    while True:
        messages[0]["content"] = _build_system()
        resp = await _llm_call(llm, tg, model=model, messages=messages, tools=_GLOSSARY_TOOLS, tool_choice="auto")
        if not resp.choices or resp.choices[0].message is None:
            reason = resp.choices[0].finish_reason if resp.choices else "no choices"
            await tg.notify(f"⚠️ LLM вернула пустой ответ (finish_reason={reason}), повторяю...")
            continue
        msg = resp.choices[0].message

        assistant_msg: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            tc_list = []
            for tc in msg.tool_calls:
                tc_dict = {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                tc_extra = getattr(tc, "model_extra", None) or {}
                sig = (tc_extra.get("extra_content", {})
                               .get("google", {})
                               .get("thought_signature"))
                if sig:
                    tc_dict["extra_content"] = {"google": {"thought_signature": sig}}
                tc_list.append(tc_dict)
            assistant_msg["tool_calls"] = tc_list
        messages.append(assistant_msg)

        if not msg.tool_calls:
            break

        tool_results: list[dict] = []
        for call in msg.tool_calls:
            fn = call.function.name
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                args = {}

            if fn == "glossary_add":
                proposal = _format_glossary_add_proposal(args["id"], args["description"])
                await tg.send(proposal)
                answer = await tg.wait_for_message()
                if _is_approval(answer):
                    glossary_add(args["id"], args["description"])
                    result = f"Добавлен ID:{args['id']}"
                else:
                    feedback, soul_note = _split_reply(answer)
                    _append_soul(soul_note)
                    accumulated_feedback.append(feedback)
                    result = _feedback_note(accumulated_feedback)

            elif fn == "glossary_update":
                old_desc = read_glossary_dict().get(args["id"], "")
                proposal = _format_glossary_update_proposal(args["id"], old_desc, args["new_description"])
                await tg.send(proposal)
                answer = await tg.wait_for_message()
                if _is_approval(answer):
                    glossary_update(args["id"], args["new_description"])
                    result = f"Обновлён ID:{args['id']}"
                else:
                    feedback, soul_note = _split_reply(answer)
                    _append_soul(soul_note)
                    accumulated_feedback.append(feedback)
                    result = _feedback_note(accumulated_feedback)

            elif fn == "ask_user":
                questions = args.get("questions", [])
                q_text = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
                await tg.send(f"❓ <b>Вопрос по словарю:</b>\n\n{q_text}")
                answer = await tg.wait_for_message()
                result, soul_note = _split_reply(answer)
                _append_soul(soul_note)
                if not result:
                    result = answer

            else:
                result = f"Неизвестный инструмент: {fn}"

            tool_results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })

        messages.extend(tool_results)


# ── Main loop ──────────────────────────────────────────────────────────────────

def _find_week_by_date(target_date: str) -> tuple[list, int] | None:
    """Найти неделю, содержащую указанную дату. Возвращает (week, year)."""
    year = int(target_date[:4])
    days = parse_diary(year)
    if not days:
        return None
    weeks = group_into_weeks(days)
    for week in weeks:
        if any(d.date_str == target_date for d in week):
            return week, year
    return None


async def main():
    import sys as _sys
    rerun_date = None
    for i, arg in enumerate(_sys.argv):
        if arg == "--rerun" and i + 1 < len(_sys.argv):
            rerun_date = _sys.argv[i + 1]

    cfg = load_config()

    tg = TgClient(
        token=cfg["bot_token"],
        allowed_user_ids=cfg.get("allowed_user_ids", []),
        proxy=cfg.get("proxy"),
    )

    proxy = cfg.get("proxy")
    llm_http = httpx.AsyncClient(
        proxy=proxy,
        timeout=120.0,
    )
    llm = AsyncOpenAI(
        api_key=cfg["api_key"],
        base_url=cfg.get("api_base_url",
                         "https://generativelanguage.googleapis.com/v1beta/openai/"),
        http_client=llm_http,
    )
    model = cfg.get("model", "gemini-3-flash-preview")

    try:
        await tg.drain_pending()
        print("Ожидание первого сообщения от пользователя...")
        await tg.wait_for_message()

        if rerun_date:
            result = _find_week_by_date(rerun_date)
            if not result:
                await tg.send(f"❌ Дата {rerun_date} не найдена в дневниках.")
                return
            week, year = result
            date_from, date_to = week[0].date_str, week[-1].date_str
            await tg.send(
                f"🔄 <b>Rerun: неделя {date_from} — {date_to}</b> ({year})\n\n"
                + "\n\n".join(_html_escape(d.text) for d in week)
            )
            await run_enrichment_loop(week, year, tg, llm, model)
            await tg.send(f"✅ <b>Rerun {date_from} — {date_to} завершён.</b>")
            return

        state = load_state()
        print(f"Старт: year={state.year}, week_idx={state.week_idx}")

        await tg.send(
            f"✅ Начинаем. Позиция: <b>{state.year}</b>, неделя <b>{state.week_idx + 1}</b>.\n\n"
            f"<b>Как это работает:</b>\n"
            f"• <code>.</code> или &lt;3 симв. — одобрить предложение\n"
            f"• Любой текст — замечание (LLM повторит с учётом)\n"
            f"• Строки с <code>+</code> в замечании → сохраняются в SOUL.md навсегда"
        )

        for year in DIARY_YEARS:
            if year < state.year:
                continue
            days = parse_diary(year)
            if not days:
                continue
            weeks = group_into_weeks(days)
            start_idx = state.week_idx if year == state.year else 0

            for week_idx in range(start_idx, len(weeks)):
                week = weeks[week_idx]
                date_from = week[0].date_str
                date_to   = week[-1].date_str

                print(f"  Неделя {date_from} — {date_to} ({week_idx+1}/{len(weeks)})")
                for day in week:
                    photos = find_day_photos(day.date_str)
                    if photos:
                        await tg.send_collage(photos)
                    text_body = day.text.removeprefix(day.date_str).lstrip()
                    await tg.send(
                        f"📖 <b>{day.date_str}</b> {_html_escape(text_body)}"
                    )

                await run_enrichment_loop(week, year, tg, llm, model)

                # Короткая статистика вместо полного саммари (текст пользователь уже видел в предложении)
                enrichments = load_enrichments(year)
                used_ids: set[str] = set()
                for ds in (d.date_str for d in week):
                    for m in _ID_RE.finditer(enrichments.get(ds, "")):
                        used_ids.add(m.group(0)[1:-1])
                await tg.send(
                    f"✅ <b>Неделя {date_from} — {date_to} сохранена.</b> "
                    f"Использовано ID: {len(used_ids)}."
                )
                state.year = year
                state.week_idx = week_idx + 1
                save_state(state)

            # Год закончился
            state.year = year + 1
            state.week_idx = 0
            save_state(state)

        await tg.send("✅ Все дневники обработаны!")

    finally:
        await tg.close()
        await llm_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
