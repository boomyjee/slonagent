"""migrate_fact_sources.py — переформулировка фактов без атрибуции источника.

Проходит по всем conversational-чанкам (is_real_document=0, chunk_id IS NOT NULL),
находит факты, которые не пришли напрямую от пользователя, и просит LLM
добавить в их формулировку явный источник ("Ассистент предположил...", и т.п.).

Обновляет SQLite и пересчитывает векторы LanceDB для изменённых фактов.

Использование:
    python scripts/migrate_fact_sources.py [--db memory/fact/facts.db] [--dry-run]
"""
import argparse
import asyncio
import ctypes
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from typing import Optional

# ── Путь к корню проекта ──────────────────────────────────────────────────────

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Включаем ANSI-цвета в cmd/PowerShell на Windows
if os.name == "nt":
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
sys.path.insert(0, ROOT)

# ── Промпт ───────────────────────────────────────────────────────────────────

_MIGRATION_SYSTEM_PROMPT = """You are reformulating memory facts to ensure proper source attribution.

You will receive:
1. The original text chunk (a conversation or message log)
2. A list of facts extracted from that chunk

Your task:
- Identify facts that do NOT come directly from the PRIMARY USER (the main subject of memory).
- For such facts (assistant inferences, suggestions, forwarded messages, third-party opinions):
  reformulate ONLY the leading statement to include the source, e.g.:
    "Assistant suggested that...", "Assistant noted that...", "Assistant inferred that...",
    "John (forwarded) wrote that..."
- Leave facts that ARE direct user statements unchanged (return them with the same text).

CRITICAL RULES:
1. Each fact may have metadata suffixes separated by " | " (e.g. "| When: ..." or "| Involving: ...").
   NEVER remove, alter or reorder these suffixes. Only prepend source attribution before the first " | ".
2. Do NOT fix typos, grammar or wording in the original fact text. Copy everything verbatim.
   The ONLY allowed change is adding "Assistant suggested that ...", "Assistant inferred that ...", etc.
   at the very beginning of the leading statement.

LANGUAGE: Output ALL fact texts in the SAME language as the input. Do NOT translate.

Return ONLY valid JSON:
{"facts": [{"fact_id": "...", "new_text": "..."}]}

Include ALL facts (changed or not). For unchanged facts, copy the original text as-is.
NEVER return plain text. NEVER omit the JSON wrapper."""


_MIGRATION_USER_TEMPLATE = """Original chunk:
{chunk_text}

Facts to review:
{facts_list}"""


# ── Вспомогательные функции ──────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    keys = cfg.get("keys", {})

    def resolve(value):
        if isinstance(value, str) and value.startswith("$keys."):
            return keys.get(value[len("$keys."):], value)
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value

    fact_cfg = cfg.get("agent", {}).get("memory_providers", {}).get("fact", {})
    return {
        "api_key":   resolve(fact_cfg.get("api_key",  keys.get("llm", ""))),
        "base_url":  resolve(fact_cfg.get("base_url", keys.get("llm_url", ""))),
        "model":     fact_cfg.get("model_name", "gemini-3-flash-preview"),
        "embedding": resolve(fact_cfg.get("embedding_model", None)),
        "env":       cfg.get("env", {}),
    }


def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_conversation_chunks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Возвращает уникальные chunk_id для conversational фактов."""
    return conn.execute("""
        SELECT DISTINCT chunk_id
        FROM facts
        WHERE is_real_document = 0
          AND chunk_id IS NOT NULL
          AND context = 'conversation'
        ORDER BY chunk_id
    """).fetchall()


def get_facts_for_chunk(conn: sqlite3.Connection, chunk_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT fact_id, fact FROM facts WHERE chunk_id = ?", (chunk_id,)
    ).fetchall()


def get_chunk_text(conn: sqlite3.Connection, chunk_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT chunk_text FROM chunks WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    return row["chunk_text"] if row else None


def update_fact_text(conn: sqlite3.Connection, fact_id: str, new_text: str) -> None:
    conn.execute("UPDATE facts SET fact = ? WHERE fact_id = ?", (new_text, fact_id))


# ── LLM ──────────────────────────────────────────────────────────────────────

async def call_llm(client, model: str, chunk_text: str, facts: list[sqlite3.Row]) -> dict:
    facts_list = "\n".join(
        f'[{f["fact_id"]}] {f["fact"]}' for f in facts
    )
    user_msg = _MIGRATION_USER_TEMPLATE.format(
        chunk_text=chunk_text,
        facts_list=facts_list,
    )
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "user",      "content": _MIGRATION_SYSTEM_PROMPT},
            {"role": "assistant", "content": "Understood. Send me the chunk and facts."},
            {"role": "user",      "content": user_msg},
        ],
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or ""
    m = re.search(r"\{", raw)
    if not m:
        return {}
    data, _ = json.JSONDecoder().raw_decode(raw, m.start())
    return data


# ── Embedding ─────────────────────────────────────────────────────────────────

def make_embedder(embedding_cfg):
    """Создаёт embedder по конфигу из .config.json."""
    if not embedding_cfg:
        from src.memory.providers.fact.storage import LocalEmbedder, DEFAULT_EMBEDDING_MODEL
        return LocalEmbedder(DEFAULT_EMBEDDING_MODEL)
    if isinstance(embedding_cfg, str):
        from src.memory.providers.fact.storage import LocalEmbedder
        return LocalEmbedder(embedding_cfg)
    from src.memory.providers.fact.storage import OpenAIEmbedder
    return OpenAIEmbedder(
        model=embedding_cfg["model"],
        api_key=embedding_cfg["api_key"],
        base_url=embedding_cfg["base_url"],
    )


def update_lancedb_vectors(lancedb_path: str, embedder, updated: list[dict]) -> None:
    """Батчевое обновление векторов. updated: [{"fact_id", "fact", "occurred_start", "mentioned_at"}]"""
    if not updated:
        return
    try:
        import lancedb
        from src.memory.providers.fact.retain import augment_text_for_embedding, Fact
        db = lancedb.connect(lancedb_path)
        if "facts" not in db.table_names():
            return
        table = db.open_table("facts")
        facts = [
            Fact(fact_id=r["fact_id"], fact=r["fact"],
                 occurred_start=r.get("occurred_start"), mentioned_at=r.get("mentioned_at"))
            for r in updated
        ]
        augmented = [augment_text_for_embedding(f) for f in facts]
        vectors = embedder.encode_texts(augmented)
        for f, vec in zip(facts, vectors):
            try:
                table.delete(f"fact_id = '{f.fact_id}'")
            except Exception:
                pass
        table.add([
            {"fact_id": f.fact_id, "mentioned_at": f.mentioned_at or "", "vector": vec}
            for f, vec in zip(facts, vectors)
        ])
    except Exception as e:
        print(f"  ⚠ LanceDB batch update failed: {e}")


# ── HTML report ───────────────────────────────────────────────────────────────

_HTML_HEAD = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Миграция источников фактов</title>
<style>
  body { font-family: sans-serif; max-width: 960px; margin: 40px auto; padding: 0 20px; background: #f9f9f9; }
  h1   { font-size: 1.3em; color: #333; }
  .chunk { margin-bottom: 28px; border: 1px solid #ddd; border-radius: 6px; background: #fff; padding: 16px; }
  details summary { cursor: pointer; color: #888; font-size: 0.85em; margin-bottom: 10px; }
  details pre { white-space: pre-wrap; font-size: 0.82em; color: #999; background: #f4f4f4;
                padding: 10px; border-radius: 4px; margin: 8px 0 12px; }
  .fact { margin: 8px 0; }
  .was  { background: #fff0f0; border-left: 3px solid #e88; padding: 6px 10px;
          border-radius: 3px; font-size: 0.9em; color: #a00; margin-bottom: 3px; }
  .now  { background: #f0fff0; border-left: 3px solid #8b8; padding: 6px 10px;
          border-radius: 3px; font-size: 0.9em; color: #060; }
  .label { font-size: 0.75em; font-weight: bold; color: #999; margin-bottom: 2px; }
  footer { color: #999; font-size: 0.85em; margin-top: 30px; }
</style>
</head>
<body>
<h1>Миграция источников фактов</h1>
"""

_HTML_FOOT = """<footer>Отчёт завершён.</footer>
</body></html>
"""

_HTML_INTERRUPTED = """<footer style="color:#c00">⚠ Скрипт был прерван — отчёт неполный.</footer>
</body></html>
"""


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class HtmlReport:
    def __init__(self, path: str):
        self._f = open(path, "w", encoding="utf-8")
        self._f.write(_HTML_HEAD)
        self._f.flush()
        self._path = path

    def write_chunk(self, chunk_id: str, chunk_text: str, changes: list[tuple[str, str, str]]):
        lines = [f'<div class="chunk">']
        lines.append(f'<details><summary>Чанк {_esc(chunk_id)}</summary>'
                     f'<pre>{_esc(chunk_text)}</pre></details>')
        for _, old_text, new_text in changes:
            lines.append('<div class="fact">')
            lines.append(f'<div class="label">БЫЛО</div><div class="was">{_esc(old_text)}</div>')
            lines.append(f'<div class="label">СТАЛО</div><div class="now">{_esc(new_text)}</div>')
            lines.append('</div>')
        lines.append('</div>')
        self._f.write("\n".join(lines) + "\n")
        self._f.flush()

    def close(self, interrupted: bool = False):
        self._f.write(_HTML_INTERRUPTED if interrupted else _HTML_FOOT)
        self._f.close()
        print(f"HTML-отчёт: {self._path}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Migrate fact source attribution")
    parser.add_argument("--db",      default=os.path.join(ROOT, "memory", "fact", "facts.db"),
                        help="Path to facts.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show changes without saving")
    parser.add_argument("--config",  default=os.path.join(ROOT, ".config.json"),
                        help="Path to .config.json")
    report_path = os.path.join(ROOT, "migration_report.html")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"[error] DB not found: {args.db}")
        sys.exit(1)

    cfg = load_config(args.config)

    for k, v in cfg["env"].items():
        os.environ.setdefault(k, v)

    import httpx
    from openai import AsyncOpenAI
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    http_client = httpx.AsyncClient(proxy=proxy_url, timeout=120.0)
    llm = AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], http_client=http_client)

    lancedb_path = os.path.join(os.path.dirname(args.db), "lancedb")
    embedder = make_embedder(cfg["embedding"]) if not args.dry_run else None

    conn = open_db(args.db)
    chunks = get_conversation_chunks(conn)
    print(f"Найдено conversational чанков: {len(chunks)}")
    if not chunks:
        print("Нечего мигрировать.")
        return

    report = HtmlReport(report_path)
    total_changed = 0

    try:
        for chunk_row in chunks:
            chunk_id = chunk_row["chunk_id"]
            chunk_text = get_chunk_text(conn, chunk_id)
            facts = get_facts_for_chunk(conn, chunk_id)

            if not facts:
                continue
            if not chunk_text:
                print(f"\n[{chunk_id}] — chunk_text не найден, пропускаем")
                continue

            print(f"\n── Чанк {chunk_id} ({len(facts)} фактов) ──")

            try:
                result = await call_llm(llm, cfg["model"], chunk_text, facts)
            except Exception as e:
                print(f"  ⚠ LLM ошибка: {e}")
                continue

            new_facts = {item["fact_id"]: item["new_text"] for item in result.get("facts", [])}
            fact_map  = {f["fact_id"]: f for f in facts}

            changed_in_chunk = [
                (fact_id, fact_map[fact_id]["fact"], new_text)
                for fact_id, new_text in new_facts.items()
                if fact_id in fact_map and new_text.strip() != fact_map[fact_id]["fact"].strip()
            ]

            if changed_in_chunk:
                print(f"\x1b[90m{chunk_text}\x1b[0m")

            for fact_id, old_text, new_text in changed_in_chunk:
                print(f"\n  БЫЛО:  {old_text}")
                print(f"  СТАЛО: {new_text}")
                total_changed += 1

            if changed_in_chunk:
                report.write_chunk(chunk_id, chunk_text, changed_in_chunk)
                if not args.dry_run:
                    lancedb_batch = []
                    for fact_id, _, new_text in changed_in_chunk:
                        update_fact_text(conn, fact_id, new_text)
                        row = conn.execute(
                            "SELECT occurred_start, mentioned_at FROM facts WHERE fact_id = ?", (fact_id,)
                        ).fetchone()
                        lancedb_batch.append({
                            "fact_id":        fact_id,
                            "fact":           new_text,
                            "occurred_start": row["occurred_start"] if row else None,
                            "mentioned_at":   row["mentioned_at"]   if row else None,
                        })
                    conn.commit()
                    update_lancedb_vectors(lancedb_path, embedder, lancedb_batch)

        report.close()

    except (KeyboardInterrupt, Exception):
        report.close(interrupted=True)
        raise

    if not args.dry_run:
        conn.commit()

    conn.close()
    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Итого изменено фактов: {total_changed}")


if __name__ == "__main__":
    asyncio.run(main())
