import json
import re
from pathlib import Path

from src.modes.diary_enrichment.diary import Diary, DiaryDay
from src.modes.diary_enrichment.glossary import Glossary

_ID_RE = re.compile(r'\[ID:[^\]]+\]')


class EnrichmentStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    @staticmethod
    def strip_ids(text: str) -> str:
        return _ID_RE.sub("", text)

    def validate(self, original: str, annotated: str) -> tuple[bool, str]:
        stripped = self.strip_ids(annotated)
        if stripped == original:
            return True, ""
        a, b = original, stripped
        for i, (ca, cb) in enumerate(zip(a, b)):
            if ca != cb:
                ctx_a = repr(a[max(0, i - 10):i + 20])
                ctx_b = repr(b[max(0, i - 10):i + 20])
                return False, f"First diff at pos {i}: original={ctx_a}, stripped={ctx_b}"
        if len(a) != len(b):
            return False, f"Length: original={len(a)}, stripped={len(b)}"
        return False, "Unknown diff"

    def load(self, year: int) -> dict[str, str]:
        path = self.data_dir / f"enrichment_{year}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, year: int, data: dict[str, str]):
        path = self.data_dir / f"enrichment_{year}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_old_hints(self, year: int) -> dict[str, list[str]]:
        path = self.data_dir / f"old_enchiment_{year}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def get_prev_weeks_context(self, week: list[DiaryDay], year: int, n: int = 2) -> str:
        first_date = week[0].date_str
        enc = self.load(year)
        prev_days = sorted(ds for ds in enc if ds < first_date)
        if not prev_days:
            return ""
        tail = prev_days[-(n * 7):]
        lines = [f"--- LAST {n} WEEKS (saved enrichment, for context) ---"]
        for ds in tail:
            lines.append(enc[ds])
        return "\n\n".join(lines)

    def build_compiled(self, year: int, diary: Diary, glossary: Glossary):
        days = diary.parse_year(year)
        enrichments = self.load(year)
        glossary_dict = glossary.read_dict()

        out: list[str] = []
        for day in days:
            annotated = enrichments.get(day.date_str)
            out.append(annotated if annotated else day.text)

            if annotated:
                used: list[tuple[str, str]] = []
                seen: set[str] = set()
                for m in _ID_RE.finditer(annotated):
                    id_full = m.group(0)[1:-1]
                    if id_full in seen:
                        continue
                    seen.add(id_full)
                    key = id_full[3:]
                    desc = glossary_dict.get(key, "?")
                    used.append((id_full, desc))
                if used:
                    out.append("")
                    out.append("  Decryption:")
                    for id_full, desc in used:
                        out.append(f"  * {id_full} -- {desc}")

            out.append("")

        path = self.data_dir / f"compiled_{year}.txt"
        path.write_text("\n".join(out), encoding="utf-8")
        return path
