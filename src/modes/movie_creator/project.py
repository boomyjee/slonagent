"""Movie project data model — plain dataclasses, nothing else.

Persistence and mutation live in the server. This module exposes:
  - the dataclass tree (Generation/Shot/Scene/Character/Project)
  - pure helpers that read the tree (resolve_path, resolve_entity, dump)
  - load_project / save_project as free functions (the server calls them)

Ordering invariant for nested ``dict[str, Entity]``: **insertion order is
display order**. Callers append on create and rebuild the dict on reorder —
nothing in this module sorts anything.
"""
import json
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
class Project:
    title: str = ""
    next_id: int = 1
    scenes: dict[str, Scene] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)


# Top-level collection → (entity class, nested-slot → child class map)
_SCHEMA: dict[str, tuple[type, dict[str, type]]] = {
    "scenes":     (Scene,     {"shots": Shot}),
    "characters": (Character, {"generations": Generation}),
}
_NESTED_OF: dict[type, dict[str, type]] = {
    Scene:      {"shots": Shot},
    Shot:       {"generations": Generation},
    Character:  {"generations": Generation},
    Generation: {},
}


def allocate_id(project: Project) -> str:
    eid = str(project.next_id)
    project.next_id += 1
    return eid


def resolve_path(project: Project, path: list[str]) -> tuple[dict | None, type | None]:
    """Resolve an odd-length path to (container_dict, entity_class)."""
    if not path or path[0] not in _SCHEMA:
        return None, None
    cls, nested = _SCHEMA[path[0]]
    container = getattr(project, path[0])
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
        nested = _NESTED_OF.get(cls, {})
        i += 2
    if len(path) % 2 == 0:
        return None, None
    return container, cls


def resolve_entity(project: Project, path: list[str]) -> Any | None:
    """Resolve an even-length path to a specific entity."""
    if len(path) < 2 or len(path) % 2 != 0:
        return None
    container, _ = resolve_path(project, path[:-1])
    return container.get(path[-1]) if container else None


def load_project(path: Path) -> Project:
    if not path.exists():
        return Project()
    return from_dict(Project, json.loads(path.read_text(encoding="utf-8")))


def save_project(project: Project, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(project), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
