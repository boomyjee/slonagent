import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_DAY_HEADER = re.compile(r'^(\d{4}\.\d{2}\.\d{2})(?:[ \t]+-[ \t]+\S+)?', re.MULTILINE)


@dataclass
class DiaryDay:
    date_str: str
    date: date
    text: str  # full raw block: header line + body, rstripped


class Diary:
    def __init__(self, data_dir: Path, years: list[int]):
        self.data_dir = data_dir
        self.years = years

    def parse_year(self, year: int) -> list[DiaryDay]:
        path = self.data_dir / f"diary_{year}.txt"
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

    def group_into_weeks(self, days: list[DiaryDay]) -> list[list[DiaryDay]]:
        """Group days by ISO weeks (mon-sun)."""
        if not days:
            return []
        weeks: list[list[DiaryDay]] = []
        current: list[DiaryDay] = []
        current_key: tuple | None = None
        for day in days:
            key = day.date.isocalendar()[:2]
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

    def find_week_by_date(self, target_date: str) -> tuple[list[DiaryDay], int] | None:
        year = int(target_date[:4])
        days = self.parse_year(year)
        if not days:
            return None
        weeks = self.group_into_weeks(days)
        for week in weeks:
            if any(d.date_str == target_date for d in week):
                return week, year
        return None
