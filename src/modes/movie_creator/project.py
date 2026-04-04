"""Movie project model — scenes, persistence, dependency tracking."""
import json
import time
from pathlib import Path


class Character:
    __slots__ = ("id", "name", "description", "appearance", "order")

    def __init__(self, id: str = "", name: str = "", description: str = "",
                 appearance: str = "", order: int = 0):
        self.id = id
        self.name = name
        self.description = description
        self.appearance = appearance
        self.order = order

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "appearance": self.appearance, "order": self.order,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Character":
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class Scene:
    __slots__ = ("id", "title", "text", "location", "characters", "order")

    def __init__(self, id: str = "", title: str = "", text: str = "",
                 location: str = "", characters: list[str] = None, order: int = 0):
        self.id = id
        self.title = title
        self.text = text
        self.location = location
        self.characters = characters or []
        self.order = order

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "text": self.text,
            "location": self.location, "characters": self.characters,
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Scene":
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class Project:
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
        data = {
            "title": self.title,
            "next_id": self._next_id,
            "scenes": {k: v.to_dict() for k, v in self.scenes.items()},
            "characters": {k: v.to_dict() for k, v in self.characters.items()},
        }
        self._project_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, project_dir: Path) -> "Project":
        path = project_dir / "project.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        proj = cls(project_dir, title=data.get("title", ""))
        proj._next_id = data.get("next_id", 1)
        for d in data.get("scenes", {}).values():
            scene = Scene.from_dict(d)
            proj.scenes[scene.id] = scene
        for d in data.get("characters", {}).values():
            char = Character.from_dict(d)
            proj.characters[char.id] = char
        return proj

    def _log(self, action: str, detail: str = ""):
        self.project_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "action": action}
        if detail:
            entry["detail"] = detail
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── scenes CRUD ──

    def create_scene(self, title: str = "", text: str = "",
                     location: str = "") -> Scene:
        sid = str(self._next_id)
        self._next_id += 1
        order = max((s.order for s in self.scenes.values()), default=-1) + 1
        scene = Scene(id=sid, title=title, text=text, location=location, order=order)
        self.scenes[sid] = scene
        self._log("create_scene", sid)
        self.save()
        return scene

    def update_scene(self, sid: str, **fields) -> Scene | None:
        scene = self.scenes.get(sid)
        if not scene:
            return None
        for k, v in fields.items():
            if hasattr(scene, k):
                setattr(scene, k, v)
        self._log("update_scene", sid)
        self.save()
        return scene

    def delete_scene(self, sid: str) -> bool:
        if sid not in self.scenes:
            return False
        del self.scenes[sid]
        self._log("delete_scene", sid)
        self.save()
        return True

    def reorder_scenes(self, order: list[str]):
        for i, sid in enumerate(order):
            if sid in self.scenes:
                self.scenes[sid].order = i
        self._log("reorder_scenes")
        self.save()

    def scenes_ordered(self) -> list[Scene]:
        return sorted(self.scenes.values(), key=lambda s: s.order)

    # ── characters CRUD ──

    def create_character(self, name: str = "", description: str = "",
                         appearance: str = "") -> Character:
        cid = str(self._next_id)
        self._next_id += 1
        order = max((c.order for c in self.characters.values()), default=-1) + 1
        char = Character(id=cid, name=name, description=description,
                         appearance=appearance, order=order)
        self.characters[cid] = char
        self._log("create_character", cid)
        self.save()
        return char

    def update_character(self, cid: str, **fields) -> Character | None:
        char = self.characters.get(cid)
        if not char:
            return None
        for k, v in fields.items():
            if hasattr(char, k):
                setattr(char, k, v)
        self._log("update_character", cid)
        self.save()
        return char

    def delete_character(self, cid: str) -> bool:
        if cid not in self.characters:
            return False
        del self.characters[cid]
        self._log("delete_character", cid)
        self.save()
        return True

    def characters_ordered(self) -> list[Character]:
        return sorted(self.characters.values(), key=lambda c: c.order)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "scenes": [s.to_dict() for s in self.scenes_ordered()],
            "characters": [c.to_dict() for c in self.characters_ordered()],
        }
