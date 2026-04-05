"""WebSocket + HTTP server for movie creator mode."""
import asyncio
import contextlib
import json
import logging
import webbrowser
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse

from src.modes.movie_creator.project import Project

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class MovieServer:
    """Path-based WebSocket API.

    Every entity in the project is addressed by a path — a list of string
    segments like ``["scenes", "3"]`` (a scene) or
    ``["scenes", "3", "shots", "2", "generations", "5"]`` (a generation).
    Odd-length paths point to a *container* (collection dict), even-length
    paths point to an *entity*. This maps 1:1 to the Python data model via
    ``Project.resolve_path`` / ``Project.resolve_entity`` — so handlers stay
    uniform regardless of depth.
    """

    def __init__(self, port: int, project: Project):
        self.port = port
        self.project = project
        self.generator = None  # set by orchestrator
        self.app = FastAPI()
        self.ws: WebSocket | None = None
        self.active_tab: str = "screenplay"
        self.active_scope: dict = {}  # tab-specific context, e.g. {"scene_id": "1"}
        self._on_chat: list = []
        self._on_tab_changed: list = []
        self._pending_approval: asyncio.Future | None = None
        self._setup_routes()

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
            full = self.project.assets_dir / path
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

        if t == "create":
            # path = container path (odd length), e.g. ["scenes"] or ["scenes","7","shots"]
            container, cls = self.project.resolve_path(path)
            if container is not None:
                self.project.create(container, cls, **msg.get("data", {}))
                await self.send_project()

        elif t == "update":
            # path = entity path (even length)
            container, _ = self.project.resolve_path(path[:-1])
            if container is not None and path[-1] in container:
                self.project.update(container, path[-1], **msg.get("data", {}))
                await self.send_project()

        elif t == "delete":
            container, _ = self.project.resolve_path(path[:-1])
            if container is not None and path[-1] in container:
                self.project.delete(container, path[-1])
                await self.send_project()

        elif t == "generate":
            owner = self.project.resolve_entity(path)
            if owner is not None:
                asyncio.create_task(self.generator.enqueue(
                    owner,
                    msg.get("kind", "portrait"),
                    msg.get("prompt", ""),
                    msg.get("media_type", "image"),
                ))

        elif t == "set_primary":
            # path = generation path, e.g. ["characters","3","generations","5"]
            gen = self.project.resolve_entity(path)
            owner = self.project.resolve_entity(path[:-2])
            if gen and owner and getattr(gen, "file", ""):
                owner.image = gen.file
                self.project.save()
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
                "project": self.project.to_dict(),
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
        """Create or update an entity at ``path``. Odd-length path = create in
        that container, even-length path = update existing entity.

        If ``approval=True``, shows the user an editable form first; for updates
        the user sees current values merged with any non-empty overrides from
        ``fields`` (so AI can pass empty strings for "don't change").
        """
        fields = {k: v for k, v in fields.items() if k != "self" and not k.startswith("_")}
        is_create = len(path) % 2 == 1

        if approval:
            if not is_create:
                obj = self.project.resolve_entity(path)
                if obj is not None:
                    # For update approvals: fall back to current value when AI passed ""
                    fields = {k: (v if v else getattr(obj, k, "")) for k, v in fields.items()}
            # kind label derived from container segment (e.g. "scenes" → "scene")
            container_seg = path[-1] if is_create else path[-2]
            kind = container_seg.rstrip("s")
            result = await self.request_approval(kind, {"path": path, "fields": fields})
            if result.get("action") == "reject":
                return {"status": "rejected", "reason": result.get("reason", "")}
            fields = result.get("data", {}) or {}
            fields = {k: v for k, v in fields.items() if not k.startswith("_")}

        if is_create:
            container, cls = self.project.resolve_path(path)
            if container is None:
                return {"status": "error", "error": f"invalid path {path}"}
            obj = self.project.create(container, cls, **fields)
            await self.send_project()
            return {"status": "created", "id": obj.id}
        else:
            container, _ = self.project.resolve_path(path[:-1])
            if container is None or path[-1] not in container:
                return {"status": "error", "error": f"not found {path}"}
            self.project.update(container, path[-1], **fields)
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
