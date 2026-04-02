import re

from src.modes.diary_enrichment.diary import DiaryDay
from src.modes.diary_enrichment.enrichment import _ID_RE
from src.modes.diary_enrichment.glossary import Glossary

_APPROVE_HINT = (
    "\n\n_Одобрить: отправь . или любой символ <3 симв._\n"
    "_Замечание: напиши текст. Строки с + в начале сохраняются в SOUL.md._"
)


def highlight_ids(text: str) -> str:
    parts = []
    last = 0
    for m in _ID_RE.finditer(text):
        parts.append(text[last:m.start()])
        parts.append(f"`{m.group(0)}`")
        last = m.end()
    parts.append(text[last:])
    return "".join(parts)


def is_approval(text: str) -> bool:
    """Short message (< 3 chars) counts as approval."""
    return len(text.strip()) < 3


def split_reply(text: str) -> tuple[str, str]:
    """Split user reply into (feedback, soul_note).

    Lines starting with '+' go to both feedback (without prefix) and soul_note.
    Lines without '+' go only to feedback.
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


def feedback_note(feedback: list[str]) -> str:
    """Format accumulated feedback for returning to LLM."""
    items = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(feedback))
    return f"ОТКЛОНЕНО пользователем.\n\nВсе замечания:\n{items}\n\nИсправь и повтори вызов."


def format_enrich_proposal(week: list[DiaryDay], proposed_days: dict[str, str], glossary: Glossary) -> str:
    glossary_dict = glossary.read_dict()
    lines = ["**Предлагаемое обогащение:**\n"]
    for day in week:
        annotated = proposed_days.get(day.date_str)
        if not annotated:
            continue
        text_body = annotated.removeprefix(day.date_str).lstrip()
        lines.append(f"**{day.date_str}** {highlight_ids(text_body)}")
        used: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in _ID_RE.finditer(annotated):
            id_full = m.group(0)[1:-1]
            if id_full in seen:
                continue
            seen.add(id_full)
            key = id_full[3:]
            used.append((id_full, glossary_dict.get(key, "?")))
        if used:
            lines.append("")
            for id_full, desc in used:
                lines.append(f"  `{id_full}` -- {desc}")
        lines.append("")
    return "\n".join(lines) + _APPROVE_HINT


def format_glossary_add_proposal(id_key: str, description: str) -> str:
    return (
        f"**Добавить в глоссарий:**\n"
        f"`ID:{id_key}: {description}`"
        + _APPROVE_HINT
    )


def format_glossary_update_proposal(id_key: str, old_desc: str, new_desc: str) -> str:
    return (
        f"**Изменить в глоссарии:**\n"
        f"`ID:{id_key}`\n"
        f"Было: {old_desc}\n"
        f"Станет: {new_desc}"
        + _APPROVE_HINT
    )


def build_week_summary(week: list[DiaryDay], year: int, enrichments: dict[str, str], glossary: Glossary) -> str:
    glossary_dict = glossary.read_dict()
    date_from = week[0].date_str
    date_to = week[-1].date_str

    lines = [f"**Неделя: {date_from} -- {date_to}**\n"]
    for day in week:
        annotated = enrichments.get(day.date_str, day.text)
        lines.append(f"**{day.date_str}**")
        lines.append(annotated)

        used: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in _ID_RE.finditer(annotated):
            id_full = m.group(0)[1:-1]
            if id_full in seen:
                continue
            seen.add(id_full)
            key = id_full[3:]
            used.append((id_full, glossary_dict.get(key, "?")))

        if used:
            lines.append("")
            lines.append("  _Расшифровка:_")
            for id_full, desc in used:
                lines.append(f"  * {id_full} -- {desc}")
        lines.append("")

    return "\n".join(lines)
