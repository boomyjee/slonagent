"""Movie Creator web transport — serves the project editor UI under /{agent_id}/movie/.

Owns project state (load/save), generator, asset thumbnails, approval dialog
plumbing, and the movie-specific wire protocol. Chat goes through the shared
WebTransport process_message flow; movie-specific events (create/update/delete/
generate/approval_response/tab_changed/selected_changed) are handled in
ws_handle_message and mirrored back to clients via project_updated /
approval_request events.

Path-based protocol — every entity is addressed by a list of segments,
e.g. ``["scenes", "3"]`` (a scene) or
``["scenes", "3", "shots", "2", "generations", "5"]`` (a generation).
A path ending on a dataclass field name resolves to that container;
a path ending on an id inside a container resolves to the entity.
"""
import asyncio
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path

from dacite import from_dict
from fastapi import Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from src.modes.movie_creator.project import Generation, Project
from src.transport.web import WebTransport, WebFork

log = logging.getLogger(__name__)


class MovieFork(WebFork):
    prefix = "/movie"

    def __init__(self, ref_agent):
        self.project_dir: Path | None = None
        self.project_path: Path | None = None
        self.assets_dir: Path | None = None
        self.project = Project()
        self.generator = None
        self.active_tab: str = "screenplay"
        self.selected_path: list | None = None
        self._pending_approval: asyncio.Future | None = None
        self._on_tab_changed: list = []
        super().__init__(ref_agent)

    def load(self, project_dir: Path):
        """Point the fork at a project dir and load project.json if present."""
        self.project_dir = project_dir
        self.project_path = project_dir / "project.json"
        self.assets_dir = project_dir / "assets"
        if self.project_path.exists():
            self.project = from_dict(Project, json.loads(self.project_path.read_text(encoding="utf-8")))

    # --- WebFork hooks ------------------------------------------------------

    def register_routes(self):
        self.register_route("get", "/api/asset/{path:path}", self._api_asset)
        self.register_route("post", "/api/upload", self._api_upload)
        super().register_routes()

    async def ws_connect(self, ws: WebSocket):
        # Prime the new client before super() enters its receive loop —
        # self.send() goes through self.clients, which this ws isn't in yet.
        await ws.send_text(json.dumps(
            {"type": "project_updated", "project": asdict(self.project)},
            ensure_ascii=False,
        ))
        await super().ws_connect(ws)

    async def ws_handle_message(self, msg: dict, ws=None):
        t = msg.get("type")
        path = msg.get("path") or []
        data = msg.get("data") or {}

        if t in ("create", "update", "delete"):
            if getattr(self.project, t)(path, data):
                await self.save()
        elif t == "generate" and self.generator is not None:
            owner = self.project.resolve(path)
            if owner is not None and not isinstance(owner, dict):
                ref_files = msg.get("references") or []
                ref_paths = [self.assets_dir / f for f in ref_files]
                asyncio.create_task(self.generator.enqueue(
                    owner,
                    msg.get("kind", "portrait"),
                    msg.get("prompt", ""),
                    model=msg.get("model", "gemini-image"),
                    references=ref_paths,
                    duration=msg.get("duration", 5),
                    aspect_ratio=msg.get("aspect_ratio", "16:9"),
                    resolution=msg.get("resolution", "720p"),
                    fast=msg.get("fast", False),
                ))
        elif t == "approval_response":
            if self._pending_approval and not self._pending_approval.done():
                self._pending_approval.set_result(msg)
        elif t == "tab_changed":
            new_tab = msg.get("tab", "screenplay")
            if new_tab != self.active_tab:
                self.active_tab = new_tab
                for cb in self._on_tab_changed:
                    cb(new_tab)
        elif t == "selected_changed":
            self.selected_path = msg.get("path")
        else:
            await super().ws_handle_message(msg, ws)

    # --- Project helpers ----------------------------------------------------

    async def save(self):
        self.project_path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self.project)
        self.project_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await self.send({"type": "project_updated", "project": data})

    async def send_approval(self, kind: str, data: dict) -> dict:
        """Ask user to approve/edit/reject. Returns {action, data?, reason?}."""
        self._pending_approval = asyncio.get_event_loop().create_future()
        await self.send({"type": "approval_request", "kind": kind, "data": data})
        try:
            return await self._pending_approval
        finally:
            self._pending_approval = None

    async def edit(self, path: list, fields: dict, *, approval_kind: str = "") -> dict:
        """Create or update an entity at ``path``, optionally with user approval.

        If ``approval_kind`` is set, user sees an approval dialog before the change.
        Path ending on a container field = create; on an id = update.
        """
        fields = {k: v for k, v in fields.items() if k != "self"}
        target = self.project.resolve(path)
        is_create = isinstance(target, dict)

        if approval_kind:
            if not is_create and target is not None:
                fields = {k: (v if v else getattr(target, k, "")) for k, v in fields.items()}
            result = await self.send_approval(approval_kind, {"path": path, "fields": fields})
            if result.get("action") == "reject":
                return {"status": "rejected", "reason": result.get("reason", "")}
            fields = result.get("data") or {}

        if is_create:
            eid = self.project.create(path, fields)
            if eid is None:
                return {"status": "error", "error": f"invalid path {path}"}
            await self.save()
            return {"status": "created", "id": eid}
        if not self.project.update(path, fields):
            return {"status": "error", "error": f"not found {path}"}
        await self.save()
        return {"status": "updated", "id": path[-1]}

    def on_tab_changed(self, callback):
        self._on_tab_changed.append(callback)

    # --- Asset serving ------------------------------------------------------

    def _make_thumbnail(self, original: Path, thumb: Path, width: int, height: int):
        thumb.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(original) as img:
            img.thumbnail((width, height), Image.LANCZOS)
            img.save(thumb, quality=85)

    async def _api_asset(self, path: str):
        img_cache = {"Cache-Control": "max-age=86400"}
        m = re.match(r"^(\d+)x(\d+)/(.+)$", path)
        if m:
            width, height, rel = int(m.group(1)), int(m.group(2)), m.group(3)
            original = self.assets_dir / rel
            if not original.exists():
                return JSONResponse({"error": "Not found"}, 404)
            if original.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                return FileResponse(original, headers=img_cache)
            thumb = self.assets_dir / ".thumbs" / f"{width}x{height}" / rel
            if not thumb.exists() or thumb.stat().st_mtime < original.stat().st_mtime:
                self._make_thumbnail(original, thumb, width, height)
            return FileResponse(thumb, headers=img_cache)
        full = self.assets_dir / path
        if not full.exists():
            return JSONResponse({"error": "Not found"}, 404)
        return FileResponse(full, headers=img_cache)

    async def _api_upload(self, request: Request):
        form = await request.form()
        file = form.get("file")
        path = form.get("path", "") or request.query_params.get("path", "")
        kind = form.get("kind", "") or request.query_params.get("kind", "")
        if not isinstance(file, UploadFile):
            return JSONResponse({"error": "no file"}, 400)
        segments = [s for s in path.split("/") if s]
        owner = self.project.resolve(segments)
        if owner is None or isinstance(owner, dict) or not hasattr(owner, "generations"):
            return JSONResponse({"error": "invalid path"}, 400)
        data = await file.read()
        ext = file.filename.rsplit(".", 1)[-1] if "." in (file.filename or "") else "png"
        gen = Generation(
            id=self.project.allocate_id(),
            kind=kind,
            media_type="image",
            prompt="(uploaded)",
            status="done",
        )
        gen.file = f"gen_{gen.id}.{ext}"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        (self.assets_dir / gen.file).write_bytes(data)
        owner.generations[gen.id] = gen
        if hasattr(owner, "primary_generation_id") and not owner.primary_generation_id:
            owner.primary_generation_id = gen.id
        await self.save()
        return JSONResponse({"id": gen.id})


class MovieTransport(WebTransport):
    def __init__(self):
        super().__init__(verbose=False)

    def create_fork(self, agent):
        return MovieFork(ref_agent=agent)
