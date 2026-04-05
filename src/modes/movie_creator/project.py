"""Movie project model — scenes, persistence, dependency tracking."""
import json
import time
from pathlib import Path


class Entity:
    __slots__ = ("id", "order")
    _defaults = {"order": 0}

    def __init__(self, **kw):
        for slot in self.__slots__:
            val = kw.get(slot, self._defaults.get(slot, ""))
            setattr(self, slot, val() if callable(val) else val)

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class Character(Entity):
    __slots__ = ("id", "name", "description", "appearance", "image", "order")


class Scene(Entity):
    __slots__ = ("id", "title", "text", "location", "order")


class Project:
    _entity_types = {"scenes": Scene, "characters": Character}

    def __init__(self, project_dir: Path, title: str = ""):
        self.project_dir = project_dir
        self.title = title
        self.scenes: dict[str, Scene] = {}
        self.characters: dict[str, Character] = {}
        self._next_id = 1
        self._project_path = project_dir / "project.json"
        self._history_path = project_dir / "history.jsonl"
        self.assets_dir = project_dir / "assets"

    # ── persistence ──

    def save(self):
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(exist_ok=True)
        data = {"title": self.title, "next_id": self._next_id}
        for name in self._entity_types:
            data[name] = {k: v.to_dict() for k, v in getattr(self, name).items()}
        self._project_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, project_dir: Path) -> "Project":
        path = project_dir / "project.json"
        if not path.exists():
            proj = cls(project_dir)
            proj.save()
            return proj
        data = json.loads(path.read_text(encoding="utf-8"))
        proj = cls(project_dir, title=data.get("title", ""))
        proj._next_id = data.get("next_id", 1)
        for name, entity_cls in cls._entity_types.items():
            for d in data.get(name, {}).values():
                obj = entity_cls.from_dict(d)
                getattr(proj, name)[obj.id] = obj
        return proj

    def _log(self, action: str, detail: str = ""):
        self.project_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "action": action}
        if detail:
            entry["detail"] = detail
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── generic CRUD ──

    def create(self, collection: str, **fields):
        items = getattr(self, collection)
        eid = str(self._next_id)
        self._next_id += 1
        order = max((x.order for x in items.values()), default=-1) + 1
        obj = self._entity_types[collection](id=eid, order=order, **fields)
        items[eid] = obj
        self._log(f"create_{collection}", eid)
        self.save()
        return obj

    def update(self, collection: str, eid: str, **fields):
        obj = getattr(self, collection).get(eid)
        if not obj:
            return None
        for k, v in fields.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        self._log(f"update_{collection}", eid)
        self.save()
        return obj

    def delete(self, collection: str, eid: str) -> bool:
        items = getattr(self, collection)
        if eid not in items:
            return False
        del items[eid]
        self._log(f"delete_{collection}", eid)
        self.save()
        return True

    def reorder(self, collection: str, order: list[str]):
        items = getattr(self, collection)
        for i, eid in enumerate(order):
            if eid in items:
                items[eid].order = i
        self._log(f"reorder_{collection}")
        self.save()

    def ordered(self, collection: str) -> list:
        return sorted(getattr(self, collection).values(), key=lambda x: x.order)

    def to_dict(self) -> dict:
        result = {"title": self.title}
        for name in self._entity_types:
            result[name] = [x.to_dict() for x in self.ordered(name)]
        return result
