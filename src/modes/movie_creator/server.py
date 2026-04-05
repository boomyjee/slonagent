"""WebSocket + HTTP server for movie creator mode."""
import asyncio
import contextlib
import json
import logging
import webbrowser
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse

from src.modes.movie_creator.project import (
    Project, allocate_id, resolve_path, resolve_entity, save_project,
)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class MovieServer:
    """Owns the project data, persistence, and the WebSocket API.

    Path-based protocol — every entity is addressed by a list of segments,
    e.g. ``["scenes", "3"]`` (a scene) or
    ``["scenes", "3", "shots", "2", "generations", "5"]`` (a generation).
    Odd-length paths point to a container (collection dict),
    even-length paths point to a single entity.
    """

    def __init__(self, port: int, project: Project, project_dir: Path):
        self.port = port
        self.project = project
        self.project_dir = project_dir
        self.project_path = project_dir / "project.json"
        self.assets_dir = project_dir / "assets"
        self.generator = None  # set by orchestrator
        self.app = FastAPI()
        self.ws: WebSocket | None = None
        self.active_tab: str = "screenplay"
        self.active_scope: dict = {}
        self._on_chat: list = []
        self._on_tab_changed: list = []
        self._pending_approval: asyncio.Future | None = None
        self._setup_routes()

    def save(self):
        save_project(self.project, self.project_path)

    def _setup_routes(self):
        no_cache = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

        @self.app.get("/")
        async def index():
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(html, headers=no_cache)

        @self.app.get("/{filepath:path}.js")
        async def js_file(filepath: str):
            path = WEB_DIR / f"{filepath}.js"
            if ".." in filepath or not path.exists():
                return {"error": "not found"}, 404
            return FileResponse(path, media_type="application/javascript", headers=no_cache)

        @self.app.get("/api/asset/{path:path}")
        async def get_asset(path: str):
            full = self.assets_dir / path
            if not full.exists():
                return {"error": "Not found"}, 404
            return FileResponse(full)

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await ws.accept()
            self.ws = ws
            await self.send_project()
            try:
                while True:
                    data = await ws.receive_text()
                    try:
                        await self._handle_message(json.loads(data))
                    except Exception:
                        log.exception("[movie] error handling WS message: %s", data[:200])
            except WebSocketDisconnect:
                self.ws = None

    async def _handle_message(self, msg: dict):
        t = msg.get("type")
        path = msg.get("path") or []
        data = msg.get("data") or {}

        if t == "create":
            container, cls = resolve_path(self.project, path)
            if container is not None:
                eid = allocate_id(self.project)
                container[eid] = cls(id=eid, **data)
                self.save()
                await self.send_project()

        elif t == "update":
            obj = resolve_entity(self.project, path)
            if obj is not None:
                for k, v in data.items():
                    if hasattr(obj, k):
                        setattr(obj, k, v)
                self.save()
                await self.send_project()

        elif t == "delete":
            container, _ = resolve_path(self.project, path[:-1])
            if container is not None and path[-1] in container:
                del container[path[-1]]
                self.save()
                await self.send_project()

        elif t == "reorder":
            # path = container path, msg["order"] = new id sequence
            container, _ = resolve_path(self.project, path)
            if container is not None:
                order = msg.get("order", [])
                reordered = {eid: container[eid] for eid in order if eid in container}
                for eid, obj in container.items():
                    if eid not in reordered:
                        reordered[eid] = obj
                container.clear()
                container.update(reordered)
                self.save()
                await self.send_project()

        elif t == "generate":
            owner = resolve_entity(self.project, path)
            if owner is not None:
                asyncio.create_task(self.generator.enqueue(
                    owner,
                    msg.get("kind", "portrait"),
                    msg.get("prompt", ""),
                    msg.get("media_type", "image"),
                ))

        elif t == "set_primary":
            gen = resolve_entity(self.project, path)
            owner = resolve_entity(self.project, path[:-2])
            if gen and owner and getattr(gen, "file", ""):
                owner.image = gen.file
                self.save()
                await self.send_project()

        elif t == "approval_response":
            if self._pending_approval and not self._pending_approval.done():
                self._pending_approval.set_result(msg)

        elif t == "chat":
            for cb in self._on_chat:
                await cb(msg["text"])

        elif t == "tab_changed":
            self.active_tab = msg.get("tab", "screenplay")
            for cb in self._on_tab_changed:
                cb(self.active_tab)

        elif t == "scope_changed":
            self.active_scope = msg.get("scope", {}) or {}

    # ── send to client ──

    async def send_project(self):
        if self.ws:
            await self.ws.send_text(json.dumps({
                "type": "project_updated",
                "project": asdict(self.project),
            }))

    async def send_chat(self, text: str, role: str = "assistant", stream_id=None, final: bool = False):
        if self.ws:
            msg = {"type": "message", "role": role, "text": text}
            if stream_id is not None:
                msg["stream_id"] = stream_id
            if final:
                msg["final"] = True
            await self.ws.send_text(json.dumps(msg))

    async def send_event(self, event: str, **kwargs):
        if self.ws:
            await self.ws.send_text(json.dumps({"type": event, **kwargs}))

    async def request_approval(self, kind: str, data: dict) -> dict:
        """Ask user to approve/edit/reject. Returns {action, data?, reason?}."""
        self._pending_approval = asyncio.get_event_loop().create_future()
        await self.send_event("approval_request", kind=kind, data=data)
        result = await self._pending_approval
        self._pending_approval = None
        return result

    async def edit(self, path: list, fields: dict, approval: bool = False) -> dict:
        """Create or update an entity at ``path``, optionally with user approval.

        Odd-length path = create in that container, even-length = update existing.
        For update approvals: empty fields fall back to current values (so the AI
        can pass "" for "don't change").
        """
        fields = {k: v for k, v in fields.items() if k != "self" and not k.startswith("_")}
        is_create = len(path) % 2 == 1

        if approval:
            if not is_create:
                obj = resolve_entity(self.project, path)
                if obj is not None:
                    fields = {k: (v if v else getattr(obj, k, "")) for k, v in fields.items()}
            kind = (path[-1] if is_create else path[-2]).rstrip("s")
            result = await self.request_approval(kind, {"path": path, "fields": fields})
            if result.get("action") == "reject":
                return {"status": "rejected", "reason": result.get("reason", "")}
            fields = {k: v for k, v in (result.get("data") or {}).items() if not k.startswith("_")}

        if is_create:
            container, cls = resolve_path(self.project, path)
            if container is None:
                return {"status": "error", "error": f"invalid path {path}"}
            eid = allocate_id(self.project)
            container[eid] = cls(id=eid, **fields)
            self.save()
            await self.send_project()
            return {"status": "created", "id": eid}
        else:
            obj = resolve_entity(self.project, path)
            if obj is None:
                return {"status": "error", "error": f"not found {path}"}
            for k, v in fields.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
            self.save()
            await self.send_project()
            return {"status": "updated", "id": path[-1]}

    def on_chat(self, callback):
        self._on_chat.append(callback)

    def on_tab_changed(self, callback):
        self._on_tab_changed.append(callback)

    async def start(self):
        import uvicorn
        config = uvicorn.Config(
            self.app, host="0.0.0.0", port=self.port, log_level="warning")
        server = uvicorn.Server(config)
        # Don't steal Ctrl+C from the main dashboard uvicorn — newer uvicorn
        # uses capture_signals() instead of install_signal_handlers; two servers
        # fighting over SIGINT breaks graceful shutdown.
        server.capture_signals = lambda: contextlib.nullcontext()

        async def _serve():
            try:
                await server.serve()
            except SystemExit:
                pass

        asyncio.create_task(_serve())
        for _ in range(50):
            if server.started:
                log.info("[movie] server started on port %d", self.port)
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"Failed to start server on port {self.port}")
        webbrowser.open(f"http://localhost:{self.port}")
