"""Movie project model — scenes, persistence, dependency tracking."""
import json
import time
from pathlib import Path


class Entity:
    __slots__ = ("id", "order")
    _defaults = {"order": 0}
    _nested: dict = {}  # slot_name -> Entity subclass for list[Entity] slots

    def __init__(self, **kw):
        for slot in self.__slots__:
            val = kw.get(slot, self._defaults.get(slot, ""))
            if callable(val) and not isinstance(val, (str, int, float, list, dict)):
                val = val()
            if slot in self._nested and isinstance(val, list):
                cls = self._nested[slot]
                val = [x if isinstance(x, cls) else cls.from_dict(x) for x in val]
            setattr(self, slot, val)

    def to_dict(self) -> dict:
        result = {}
        for s in self.__slots__:
            v = getattr(self, s)
            if s in self._nested and isinstance(v, list):
                v = [x.to_dict() for x in v]
            result[s] = v
        return result

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class Generation(Entity):
    __slots__ = ("id", "kind", "media_type", "prompt", "file", "status", "error")
    _defaults = {"media_type": "image", "status": "queued"}


class Character(Entity):
    __slots__ = ("id", "name", "description", "appearance", "image", "generations", "order")
    _defaults = {"order": 0, "generations": list}
    _nested = {"generations": Generation}


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

    def next_id(self) -> str:
        eid = str(self._next_id)
        self._next_id += 1
        return eid

    # ── generic CRUD ──

    def create(self, collection: str, **fields):
        items = getattr(self, collection)
        eid = self.next_id()
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

    def dump(self, collection: str) -> str:
        """Plain text dump of a collection for LLM context."""
        items = self.ordered(collection)
        if not items:
            return "(пусто)"
        return "\n\n".join(
            "\n".join(
                f"{s}: {getattr(item, s)}"
                for s in item.__slots__
                if s != "order" and not isinstance(getattr(item, s), (list, dict))
            )
            for item in items
        )

    def to_dict(self) -> dict:
        result = {"title": self.title}
        for name in self._entity_types:
            result[name] = [x.to_dict() for x in self.ordered(name)]
        return result
