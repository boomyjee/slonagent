import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class State:
    year: int
    week_idx: int

    @staticmethod
    def load(path: Path) -> "State":
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            return State(d["year"], d["week_idx"])
        return State(year=2014, week_idx=0)

    def save(self, path: Path):
        path.write_text(
            json.dumps({"year": self.year, "week_idx": self.week_idx},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
