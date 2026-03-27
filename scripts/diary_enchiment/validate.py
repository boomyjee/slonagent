"""Валидация enrichment-файла за указанный год.

Запуск:
    .venv\\Scripts\\python scripts/diary_enchiment/validate.py 2014
"""
import json, re, sys
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).parent / "data"
_DAY_HEADER = re.compile(r'^(\d{4}\.\d{2}\.\d{2})(?:\s+-\s+\S+)?', re.MULTILINE)
_ID_RE = re.compile(r'\[ID:[^\]]+\]')
_ENTRY_RE = re.compile(r'^- ID:([^:\s]+:[^:\s]+):\s*(.*)', re.MULTILINE)


def load_glossary() -> dict[str, str]:
    text = (DATA / "glossary.md").read_text(encoding="utf-8")
    body = text.split("---", 1)[1] if "---" in text else ""
    return {k: v.strip() for k, v in _ENTRY_RE.findall(body)}


def load_originals(year: int) -> dict[str, str]:
    path = DATA / f"diary_{year}.txt"
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    matches = list(_DAY_HEADER.finditer(content))
    originals: dict[str, str] = {}
    for i, m in enumerate(matches):
        ds = m.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        raw = content[m.start():end].rstrip()
        if ds not in originals or len(raw) > len(originals[ds]):
            originals[ds] = raw
    return originals


def validate(year: int) -> bool:
    originals = load_originals(year)
    if not originals:
        print(f"diary_{year}.txt не найден или пуст")
        return False

    enc_path = DATA / f"enrichment_{year}.json"
    if not enc_path.exists():
        print(f"enrichment_{year}.json не найден")
        return False
    enrichments = json.loads(enc_path.read_text(encoding="utf-8"))
    glossary = load_glossary()

    print(f"=== {year} ===")
    print(f"Дневник: {len(originals)} дней | Enrichments: {len(enrichments)} | Глоссарий: {len(glossary)}")

    ok = True

    # 0. Полнота дневника (с 2015 — каждый день года)
    if year >= 2015:
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        all_days = set()
        d = start
        while d <= end:
            all_days.add(d.strftime("%Y.%m.%d"))
            d += timedelta(days=1)
        diary_dates = set(originals.keys())
        gaps = sorted(all_days - diary_dates)
        dupes_in_diary = []  # уже дедуплицированы в load_originals, проверяем сырой файл
        path = DATA / f"diary_{year}.txt"
        if path.exists():
            raw_dates = _DAY_HEADER.findall(path.read_text(encoding="utf-8"))
            seen = set()
            for rd in raw_dates:
                if rd in seen:
                    dupes_in_diary.append(rd)
                seen.add(rd)
        if gaps:
            print(f"FAIL  Пропуски в дневнике ({len(gaps)}): {', '.join(gaps[:10])}{'...' if len(gaps) > 10 else ''}")
            ok = False
        else:
            print(f"  OK  Все {len(all_days)} дней года в дневнике")
        if dupes_in_diary:
            print(f"FAIL  Дубли в дневнике: {', '.join(dupes_in_diary)}")
            ok = False
        else:
            print(f"  OK  Нет дублей дат")

    # 1. Пропуски enrichments
    missing = sorted(d for d in originals if d not in enrichments)
    if missing:
        print(f"FAIL  Пропущены ({len(missing)}): {', '.join(missing)}")
        ok = False
    else:
        print(f"  OK  Нет пропусков")

    # 2. Лишние
    extra = sorted(d for d in enrichments if d not in originals)
    if extra:
        print(f"FAIL  Лишние ({len(extra)}): {', '.join(extra)}")
        ok = False
    else:
        print(f"  OK  Нет лишних")

    # 3. Целостность текста
    text_fails = []
    for ds in sorted(enrichments):
        orig = originals.get(ds)
        if not orig:
            continue
        stripped = _ID_RE.sub("", enrichments[ds])
        if stripped != orig:
            for i, (ca, cb) in enumerate(zip(orig, stripped)):
                if ca != cb:
                    text_fails.append(f"  {ds}: pos {i}, orig={repr(orig[max(0,i-15):i+20])}, stripped={repr(stripped[max(0,i-15):i+20])}")
                    break
            else:
                text_fails.append(f"  {ds}: len orig={len(orig)} stripped={len(stripped)}")
    if text_fails:
        print(f"FAIL  Текст изменён ({len(text_fails)}):")
        for line in text_fails:
            print(line)
        ok = False
    else:
        print(f"  OK  Текст не изменён ({len(enrichments)}/{len(enrichments)})")

    # 4. Неизвестные ID
    unknown = set()
    for text in enrichments.values():
        for m in _ID_RE.finditer(text):
            key = m.group(0)[4:-1]
            if not key.startswith("Loc:") and key not in glossary:
                unknown.add(m.group(0))
    if unknown:
        print(f"FAIL  Неизвестные ID ({len(unknown)}):")
        for tag in sorted(unknown):
            print(f"  {tag}")
        ok = False
    else:
        print(f"  OK  Все ID в глоссарии")

    # 5. Локация в заголовке
    no_loc = [ds for ds, text in sorted(enrichments.items())
              if not re.search(r'\[ID:Loc:', text.split('\n')[0])]
    if no_loc:
        print(f"FAIL  Нет локации ({len(no_loc)}): {', '.join(no_loc)}")
        ok = False
    else:
        print(f"  OK  Локация в каждом дне")

    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python validate.py <year>")
        sys.exit(1)
    year = int(sys.argv[1])
    sys.exit(0 if validate(year) else 1)
