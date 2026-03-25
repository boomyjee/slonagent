"""retain_diary.py — загрузка личного дневника в FactProvider.

Весь файл — один документ. Кастомная chunk_fn режет по дням:
маленькие соседние дни объединяются, ни один день не разрезается.

Использование:
    python scripts/retain_diary.py --file h:/fotki_dnevnik_new/diary_2014_compiled.txt --dry-run
    python scripts/retain_diary.py --file h:/fotki_dnevnik_new/diary_2014_compiled.txt
"""
import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Константы ─────────────────────────────────────────────────────────────────

MIN_CHUNK_CHARS = 1500   # объединяем соседние дни, пока батч не достигнет этого размера


# ── Парсинг дневника ──────────────────────────────────────────────────────────

_DAY_HEADER = re.compile(r'^(\d{4}\.\d{2}\.\d{2})(?:\s+-\s+\S+)?$', re.MULTILINE)


@dataclass
class DiaryDay:
    date_str: str
    date: date
    header: str
    text: str
    glossary: str
    raw: str


@dataclass
class DiaryChunk:
    date_from: date
    date_to: date
    content: str


def _split_text_and_glossary(body: str) -> tuple[str, str]:
    """Отделяет строки [+] от основного текста.

    Сканируем с конца: первая непустая не-[+] строка — конец текста.
    Если весь body из [+] строк — text пустой.
    """
    lines = body.splitlines(keepends=True)
    split_at = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() and not lines[i].startswith('[+]'):
            split_at = i + 1
            break
    text     = "".join(lines[:split_at]).strip()
    glossary = "".join(l for l in lines[split_at:] if l.startswith('[+')).strip()
    return text, glossary


def parse_diary(path: str) -> list[DiaryDay]:
    with open(path, encoding='utf-8') as f:
        content = f.read()

    matches = list(_DAY_HEADER.finditer(content))
    raw_days: list[DiaryDay] = []

    for i, m in enumerate(matches):
        header   = m.group(0).strip()
        date_str = m.group(1)
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body     = content[m.end():body_end]

        text, glossary = _split_text_and_glossary(body)

        try:
            d = date.fromisoformat(date_str.replace('.', '-'))
        except ValueError:
            continue

        raw_days.append(DiaryDay(
            date_str=date_str,
            date=d,
            header=header,
            text=text,
            glossary=glossary,
            raw=f"{header}\n{body}".strip(),
        ))

    # Дни без текста (только словарик) — объединяем с предыдущим вхождением той же даты
    days: list[DiaryDay] = []
    seen: dict[date, int] = {}

    for day in raw_days:
        if not day.text:
            if day.date in seen and day.glossary:
                existing = days[seen[day.date]]
                merged = (existing.glossary + "\n" + day.glossary).strip() \
                    if existing.glossary else day.glossary
                days[seen[day.date]] = DiaryDay(
                    date_str=existing.date_str,
                    date=existing.date,
                    header=existing.header,
                    text=existing.text,
                    glossary=merged,
                    raw=existing.raw.rstrip() + "\n" + day.glossary,
                )
            continue

        if day.date not in seen:
            seen[day.date] = len(days)
        days.append(day)

    return days


def check_date_gaps(days: list[DiaryDay]) -> list[tuple[date, date]]:
    gaps = []
    for i in range(1, len(days)):
        if days[i].date > days[i - 1].date + timedelta(days=1):
            gaps.append((days[i - 1].date, days[i].date))
    return gaps


def build_chunks(days: list[DiaryDay]) -> list[DiaryChunk]:
    chunks: list[DiaryChunk] = []
    group: list[DiaryDay] = []
    group_size = 0

    def flush():
        if not group:
            return
        chunks.append(DiaryChunk(
            date_from=group[0].date,
            date_to=group[-1].date,
            content="\n\n".join(d.raw for d in group),
        ))
        group.clear()

    for day in days:
        day_size = len(day.raw)
        if group and group_size + day_size > MIN_CHUNK_CHARS:
            flush()
            group_size = 0
        group.append(day)
        group_size += day_size

    flush()
    return chunks




# ── Retain pipeline ───────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    with open(config_path, encoding='utf-8') as f:
        cfg = json.load(f)
    keys = cfg.get('keys', {})

    def resolve(value):
        if isinstance(value, str) and value.startswith('$keys.'):
            return keys.get(value[len('$keys.'):], value)
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value

    fact_cfg = cfg.get('agent', {}).get('memory_providers', {}).get('fact', {})
    return {
        'api_key':   resolve(fact_cfg.get('api_key',  keys.get('llm', ''))),
        'base_url':  resolve(fact_cfg.get('base_url', keys.get('llm_url', ''))),
        'model':     fact_cfg.get('model_name', 'gemini-3-flash-preview'),
        'embedding': resolve(fact_cfg.get('embedding_model', None)),
        'env':       cfg.get('env', {}),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Retain personal diary into FactProvider")
    parser.add_argument('--file',   required=True, help="Path to diary .txt file")
    parser.add_argument('--db',     default=os.path.join(ROOT, 'memory', 'fact', 'facts.db'))
    parser.add_argument('--config', default=os.path.join(ROOT, '.config.json'))
    parser.add_argument('--dry-run', action='store_true',
                        help="Show chunk plan without running LLM")
    parser.add_argument('--retain-mission', default=(
        "This is a personal diary. Extract facts about the diary author's life, "
        "experiences, thoughts, relationships, preferences and activities."
    ))
    args = parser.parse_args()

    print(f"Parsing {args.file} ...")
    days = parse_diary(args.file)
    print(f"Found {len(days)} days")

    gaps = check_date_gaps(days)
    if gaps:
        print(f"\nDate gaps ({len(gaps)}):")
        for prev, nxt in gaps:
            print(f"  {prev} -> {nxt}  ({(nxt - prev).days - 1} day(s) missing)")
    else:
        print("No date gaps - all days sequential.")

    chunks = build_chunks(days)
    total_chars = sum(len(c.content) for c in chunks)
    print(f"\nChunk plan: {len(chunks)} chunks, {total_chars} total chars")

    if args.dry_run:
        print("\n--- Chunk plan (dry run) ---")
        for i, c in enumerate(chunks):
            label = str(c.date_from) if c.date_from == c.date_to \
                    else f"{c.date_from} - {c.date_to}"
            print(f"  [{i+1:3d}] {label:25s}  {len(c.content):6d} chars")
        return

    cfg = load_config(args.config)
    for k, v in cfg['env'].items():
        os.environ.setdefault(k, v)

    from src.memory.providers.fact.retain import RetainItem, extract_facts, store_facts
    from src.memory.providers.fact.storage import Storage
    import httpx
    from openai import AsyncOpenAI

    lancedb_path = os.path.join(os.path.dirname(args.db), 'lancedb')
    storage = Storage(args.db, lancedb_path, cfg['embedding'])

    document_id = Path(args.file).stem

    existing = storage.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE document_id = ?", (document_id,)
    ).fetchone()[0]
    if existing:
        print(f"Already processed: {existing} facts for '{document_id}'. Skipping.")
        return

    proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
    http_client = httpx.AsyncClient(proxy=proxy_url, timeout=120.0)
    client = AsyncOpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'],
                         http_client=http_client, max_retries=0)

    with open(args.file, encoding='utf-8') as f:
        diary_text = f.read()

    item = RetainItem(
        content=diary_text,
        context="personal diary",
        document_id=document_id,
        retain_mission=args.retain_mission,
    )

    chunk_texts = [c.content for c in chunks]

    print(f"Extracting facts ({len(chunks)} chunks in parallel) ...")
    facts, chunk_meta = await extract_facts([item], client, cfg['model'],
                                            chunk_fn=lambda _: chunk_texts)
    if facts:
        store_facts(facts, storage, chunk_meta)

    print(f"Done. {len(facts)} facts extracted.")


if __name__ == '__main__':
    asyncio.run(main())
