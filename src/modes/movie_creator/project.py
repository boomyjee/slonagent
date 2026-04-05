"""Movie project model — scenes, shots, characters, persistence.

Data model is a plain dataclass tree; dacite handles dict → dataclass on load,
``dataclasses.asdict`` handles dataclass → dict on save. No custom (de)serialization.

Ordering invariant: for any nested ``dict[str, Entity]`` (scenes, shots,
generations...), **insertion order is the display order**. ``create`` appends,
``reorder`` rebuilds the dict in new order. Nothing sorts at save/load time.
"""
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from dacite import from_dict


@dataclass
class Generation:
    id: str = ""
    kind: str = ""
    media_type: str = "image"
    prompt: str = ""
    file: str = ""
    status: str = "queued"
    error: str = ""


@dataclass
class Shot:
    id: str = ""
    description: str = ""
    image: str = ""
    generations: dict[str, Generation] = field(default_factory=dict)


@dataclass
class Scene:
    id: str = ""
    title: str = ""
    text: str = ""
    location: str = ""
    shots: dict[str, Shot] = field(default_factory=dict)


@dataclass
class Character:
    id: str = ""
    name: str = ""
    description: str = ""
    appearance: str = ""
    image: str = ""
    generations: dict[str, Generation] = field(default_factory=dict)


@dataclass
class ProjectData:
    title: str = ""
    next_id: int = 1
    scenes: dict[str, Scene] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)


class Project:
    """File-backed facade over ProjectData with CRUD + path resolution.

    Path — list of string segments, e.g. ``["scenes", "7", "shots", "2"]``:
      - odd length → container (collection dict)
      - even length → entity
    """

    # Top-level collection → (entity class, nested slot map)
    # `nested` maps slot name → child Entity class for each entity type.
    _schema: dict[str, tuple[type, dict[str, type]]] = {
        "scenes":     (Scene,     {"shots": Shot}),
        "characters": (Character, {"generations": Generation}),
    }
    _nested_of: dict[type, dict[str, type]] = {
        Scene:     {"shots": Shot},
        Shot:      {"generations": Generation},
        Character: {"generations": Generation},
        Generation: {},
    }

    def __init__(self, project_dir: Path, title: str = ""):
        self.project_dir = project_dir
        self.data = ProjectData(title=title)
        self._project_path = project_dir / "project.json"
        self._history_path = project_dir / "history.jsonl"
        self.assets_dir = project_dir / "assets"

    # ── convenience accessors ──

    @property
    def title(self) -> str: return self.data.title

    @property
    def scenes(self) -> dict[str, Scene]: return self.data.scenes

    @property
    def characters(self) -> dict[str, Character]: return self.data.characters

    # ── persistence ──

    def save(self):
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(exist_ok=True)
        self._project_path.write_text(
            json.dumps(asdict(self.data), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, project_dir: Path) -> "Project":
        proj = cls(project_dir)
        path = project_dir / "project.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            proj.data = from_dict(ProjectData, raw)
        else:
            proj.save()
        return proj

    def _log(self, action: str, detail: str = ""):
        self.project_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "action": action}
        if detail:
            entry["detail"] = detail
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def next_id(self) -> str:
        eid = str(self.data.next_id)
        self.data.next_id += 1
        return eid

    # ── path resolution ──

    def resolve_path(self, path: list[str]) -> tuple[dict | None, type | None]:
        """Resolve an odd-length path to (container_dict, entity_class)."""
        if not path or path[0] not in self._schema:
            return None, None
        cls, nested = self._schema[path[0]]
        container = getattr(self.data, path[0])
        i = 1
        while i < len(path) - 1:
            parent = container.get(path[i])
            if parent is None:
                return None, None
            slot = path[i + 1]
            if slot not in nested:
                return None, None
            container = getattr(parent, slot)
            cls = nested[slot]
            nested = self._nested_of.get(cls, {})
            i += 2
        if len(path) % 2 == 0:
            return None, None
        return container, cls

    def resolve_entity(self, path: list[str]) -> Any | None:
        """Resolve an even-length path to a specific entity."""
        if len(path) < 2 or len(path) % 2 != 0:
            return None
        container, _ = self.resolve_path(path[:-1])
        return container.get(path[-1]) if container else None

    # ── CRUD ──

    def create(self, container: dict, cls: type, **fields) -> Any:
        eid = self.next_id()
        obj = cls(id=eid, **fields)
        container[eid] = obj
        self._log(f"create_{cls.__name__}", eid)
        self.save()
        return obj

    def update(self, container: dict, eid: str, **fields) -> Any | None:
        obj = container.get(eid)
        if obj is None:
            return None
        for k, v in fields.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        self._log(f"update_{type(obj).__name__}", eid)
        self.save()
        return obj

    def delete(self, container: dict, eid: str) -> bool:
        if eid not in container:
            return False
        cls_name = type(container[eid]).__name__
        del container[eid]
        self._log(f"delete_{cls_name}", eid)
        self.save()
        return True

    def reorder(self, container: dict, order: list[str]):
        """Rebuild the dict in the given order. Insertion order is authoritative."""
        reordered = {eid: container[eid] for eid in order if eid in container}
        # Preserve any ids not mentioned in `order` (append at the end)
        for eid, obj in container.items():
            if eid not in reordered:
                reordered[eid] = obj
        container.clear()
        container.update(reordered)
        self.save()

    # ── client-facing shape ──

    def to_dict(self) -> dict:
        return asdict(self.data)

    # ── dump for LLM context ──

    @staticmethod
    def dump(items) -> str:
        """Plain-text dump of entities for LLM context. Accepts a dict or iterable."""
        if isinstance(items, dict):
            items = list(items.values())
        if not items:
            return "(пусто)"
        blocks = []
        for item in items:
            lines = []
            for f in item.__dataclass_fields__:
                v = getattr(item, f)
                if isinstance(v, (list, dict)):
                    continue
                lines.append(f"{f}: {v}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
