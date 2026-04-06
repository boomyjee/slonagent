"""Movie project data model — plain dataclasses, nothing else.

Persistence lives in the server. Project exposes its own read helpers
(`resolve`, `allocate_id`). The module also exposes `child_class` (type
introspection) and `dump` (LLM-facing formatter).

Ordering invariant for nested ``dict[str, Entity]``: **insertion order is
display order**. Callers append on create — nothing in this module sorts.
"""
from dataclasses import dataclass, field, fields
from typing import Any, get_args, get_type_hints


@dataclass
class Generation:
    id: str = ""
    kind: str = ""
    media_type: str = "image"
    model: str = ""
    prompt: str = ""
    references: list[str] = field(default_factory=list)
    file: str = ""
    character_id: str = ""
    status: str = "generating"
    error: str = ""


@dataclass
class Shot:
    id: str = ""
    description: str = ""
    primary_generation_id: str = ""  # id of the generation shown as primary
    generations: dict[str, Generation] = field(default_factory=dict)


@dataclass
class Scene:
    id: str = ""
    title: str = ""
    text: str = ""
    location: str = ""
    shots: dict[str, Shot] = field(default_factory=dict)
    generations: dict[str, Generation] = field(default_factory=dict)


@dataclass
class Character:
    id: str = ""
    name: str = ""
    description: str = ""
    appearance: str = ""
    primary_generation_id: str = ""  # id of the generation shown as primary
    generations: dict[str, Generation] = field(default_factory=dict)


@dataclass
class Folder:
    id: str = ""
    name: str = ""
    generations: dict[str, Generation] = field(default_factory=dict)


@dataclass
class Project:
    title: str = ""
    next_id: int = 1
    scenes: dict[str, Scene] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)
    library: dict[str, Folder] = field(default_factory=dict)

    def allocate_id(self) -> str:
        eid = str(self.next_id)
        self.next_id += 1
        return eid

    def resolve(self, path: list[str]) -> Any:
        """Walk a path and return whatever sits at the end.

        Each segment is either a field name (when the current node is a
        dataclass) or a dict key (when the current node is a container).
        Callers check ``isinstance`` to tell container from entity.
        """
        obj: Any = self
        for seg in path:
            if obj is None:
                return None
            if isinstance(obj, dict):
                obj = obj.get(seg)
            else:
                obj = getattr(obj, seg, None)
        return obj

    def create(self, path: list[str], data: dict) -> str | None:
        """Create an entity inside the container at ``path``. Returns new id."""
        container = self.resolve(path)
        parent = self.resolve(path[:-1])
        if not isinstance(container, dict) or parent is None:
            return None
        cls = child_class(type(parent), path[-1])
        if cls is None:
            return None
        eid = self.allocate_id()
        valid = {f.name for f in fields(cls)} - {"id"}
        container[eid] = cls(id=eid, **{k: v for k, v in data.items() if k in valid})
        return eid

    def update(self, path: list[str], data: dict) -> bool:
        """Update fields on the entity at ``path``."""
        obj = self.resolve(path)
        if obj is None or isinstance(obj, dict):
            return False
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return True

    def delete(self, path: list[str], data: dict | None = None) -> bool:
        """Remove the entity at ``path`` from its container."""
        container = self.resolve(path[:-1])
        if not isinstance(container, dict) or path[-1] not in container:
            return False
        del container[path[-1]]
        return True


def child_class(cls: type, attr: str) -> type | None:
    """For a dataclass field typed ``dict[str, X]``, return X."""
    ann = get_type_hints(cls).get(attr)
    args = get_args(ann) if ann else ()
    return args[1] if len(args) == 2 else None


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
