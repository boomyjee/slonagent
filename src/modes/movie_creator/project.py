"""Movie project model — scenes, shots, characters, persistence."""
import json
import time
from pathlib import Path


class Entity:
    """Base entity. Nested children declared in `_nested = {slot: EntityClass}`
    are stored as dict[id -> Entity] internally, serialized as ordered lists."""
    __slots__ = ("id", "order")
    _defaults = {"order": 0}
    _nested: dict = {}

    def __init__(self, **kw):
        for slot in self.__slots__:
            val = kw.get(slot, self._defaults.get(slot, ""))
            if callable(val) and not isinstance(val, (str, int, float, list, dict)):
                val = val()
            if slot in self._nested:
                cls = self._nested[slot]
                if isinstance(val, list):
                    val = {x.id if isinstance(x, cls) else x["id"]:
                           (x if isinstance(x, cls) else cls.from_dict(x))
                           for x in val}
                elif isinstance(val, dict):
                    val = {k: (v if isinstance(v, cls) else cls.from_dict(v))
                           for k, v in val.items()}
            setattr(self, slot, val)

    def to_dict(self) -> dict:
        """Serialize nested children as ordered dicts keyed by id (insertion order = .order)."""
        result = {}
        for s in self.__slots__:
            v = getattr(self, s)
            if s in self._nested and isinstance(v, dict):
                ordered = sorted(v.values(), key=lambda x: x.order)
                v = {x.id: x.to_dict() for x in ordered}
            result[s] = v
        return result

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class Generation(Entity):
    __slots__ = ("id", "kind", "media_type", "prompt", "file", "status", "error", "order")
    _defaults = {"media_type": "image", "status": "queued", "order": 0}


class Shot(Entity):
    __slots__ = ("id", "description", "image", "generations", "order")
    _defaults = {"order": 0, "generations": dict}
    _nested = {"generations": Generation}


class Scene(Entity):
    __slots__ = ("id", "title", "text", "location", "shots", "order")
    _defaults = {"order": 0, "shots": dict}
    _nested = {"shots": Shot}


class Character(Entity):
    __slots__ = ("id", "name", "description", "appearance", "image", "generations", "order")
    _defaults = {"order": 0, "generations": dict}
    _nested = {"generations": Generation}


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
            raw = data.get(name, {})
            items = raw.values() if isinstance(raw, dict) else raw
            for d in items:
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

    def resolve_path(self, path: list[str]) -> tuple[dict, type] | tuple[None, None]:
        """Resolve a path like ["scenes"], ["scenes", "3", "shots"],
        ["characters", "1", "generations"] to (container_dict, entity_class)."""
        if not path or path[0] not in self._entity_types:
            return None, None
        container = getattr(self, path[0])
        cls = self._entity_types[path[0]]
        i = 1
        while i < len(path) - 1:
            parent = container.get(path[i])
            if not parent:
                return None, None
            slot = path[i + 1]
            if slot not in parent._nested:
                return None, None
            container = getattr(parent, slot)
            cls = parent._nested[slot]
            i += 2
        if i != len(path) and i != len(path) - 1:
            return None, None
        # Path ends either on collection (odd length) — return it,
        # or on an entity (even length) — caller wanted the entity, not supported here.
        return (container, cls) if len(path) % 2 == 1 else (None, None)

    def resolve_entity(self, path: list[str]) -> Entity | None:
        """Resolve an even-length path to a specific entity, e.g. ["characters", "3"]."""
        if len(path) < 2 or len(path) % 2 != 0:
            return None
        container, _ = self.resolve_path(path[:-1])
        return container.get(path[-1]) if container else None

    # ── generic CRUD on any container (top-level dict or nested dict) ──

    def create(self, container: dict, cls: type, **fields) -> Entity:
        eid = self.next_id()
        order = max((x.order for x in container.values()), default=-1) + 1
        obj = cls(id=eid, order=order, **fields)
        container[eid] = obj
        self._log(f"create_{cls.__name__}", eid)
        self.save()
        return obj

    def update(self, container: dict, eid: str, **fields) -> Entity | None:
        obj = container.get(eid)
        if not obj:
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
        for i, eid in enumerate(order):
            if eid in container:
                container[eid].order = i
        self.save()

    def ordered(self, container: dict) -> list:
        return sorted(container.values(), key=lambda x: x.order)

    @staticmethod
    def dump(items) -> str:
        """Plain text dump of entities for LLM context. Accepts dict, list, or any iterable."""
        if isinstance(items, dict):
            items = items.values()
        items = sorted(items, key=lambda x: x.order)
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
        """Client-facing shape: top-level entities as ordered dicts keyed by id."""
        result = {"title": self.title}
        for name in self._entity_types:
            result[name] = {x.id: x.to_dict() for x in self.ordered(getattr(self, name))}
        return result
