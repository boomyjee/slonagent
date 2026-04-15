"""strip_thought_marker.py — убрать `extra_content.google.thought:true` из
всех CONTEXT.json в проекте.

Маркер раньше попадал в финальный assistant-турн из-за бага в стрим-обработке
(см. agent.py): Gemini шлёт `thought=true` в каждом thinking-чанке, openai-
аккумулятор склеивал флаг и в финальную message с реальным ответом. Если
такой turn отдать обратно Gemini, он трактует его как внутреннее рассуждение
и теряет в истории. Сам баг чинится в стриме (`google.pop("thought", None)`),
этот скрипт чистит уже сохранённые на диск туры.

`thought_signature` НЕ трогаем — она нужна Gemini для function calling
(валидационная ошибка 4xx без неё на Gemini 3).

Использование:
    python scripts/strip_thought_marker.py [--dry-run]
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def strip_turn(turn: dict) -> bool:
    """Удалить `extra_content.google.thought` из тура. Возвращает True, если
    что-то поменялось."""
    extra = turn.get("extra_content")
    if not isinstance(extra, dict):
        return False
    google = extra.get("google")
    if not isinstance(google, dict) or "thought" not in google:
        return False
    google.pop("thought", None)
    if not google:
        extra.pop("google", None)
    if not extra:
        turn.pop("extra_content", None)
    return True


def process_file(path: Path, dry_run: bool) -> tuple[int, int]:
    """Возвращает (touched_turns, total_turns)."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
    except OSError as e:
        print(f"  ! cannot read {path}: {e}", file=sys.stderr)
        return 0, 0

    if not content:
        return 0, 0

    if content.startswith("["):
        turns = json.loads(content)
    else:
        turns = [json.loads(line) for line in content.splitlines() if line.strip()]

    touched = sum(1 for t in turns if isinstance(t, dict) and strip_turn(t))
    if touched == 0 or dry_run:
        return touched, len(turns)

    dir_ = path.parent
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=dir_, delete=False, suffix=".tmp"
    ) as f:
        for turn in turns:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")
        tmp = f.name
    os.replace(tmp, path)
    return touched, len(turns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Корень проекта (по умолчанию: cwd)")
    parser.add_argument("--dry-run", action="store_true", help="Только показать, ничего не записывать")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    files = sorted(root.rglob("CONTEXT.json"))
    if not files:
        print(f"CONTEXT.json не найдено в {root}")
        return

    print(f"Найдено файлов: {len(files)}{' (DRY RUN)' if args.dry_run else ''}\n")
    total_touched = 0
    for path in files:
        touched, total = process_file(path, args.dry_run)
        rel = path.relative_to(root)
        if touched:
            total_touched += touched
            print(f"  {rel}: очищено {touched} из {total} туров")
        else:
            print(f"  {rel}: чисто ({total} туров)")
    print(f"\nИтого: очищено туров: {total_touched}")


if __name__ == "__main__":
    main()
