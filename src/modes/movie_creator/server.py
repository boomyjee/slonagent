"""WebSocket + HTTP server for movie creator mode."""
import asyncio
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
    def __init__(self, port: int, project: Project):
        self.port = port
        self.project = project
        self.generator = None  # set by orchestrator
        self.app = FastAPI()
        self.ws: WebSocket | None = None
        self.active_tab: str = "screenplay"
        self._on_chat: list = []  # callbacks
        self._on_tab_changed: list = []  # callbacks
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
            # Send initial project state
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

        if t == "edit":
            await self.edit(msg["collection"], {"id": msg.get("id", ""), **msg.get("data", {})})

        elif t == "delete":
            self.project.delete(msg["collection"], msg["id"])
            await self.send_project()

        elif t == "reorder":
            self.project.reorder(msg["collection"], msg.get("order", []))
            await self.send_project()

        elif t == "generate":
            owner = getattr(self.project, msg["collection"]).get(msg["id"])
            if owner:
                asyncio.create_task(self.generator.enqueue(
                    owner,
                    msg.get("kind", "portrait"),
                    msg.get("prompt", ""),
                    msg.get("media_type", "image"),
                ))

        elif t == "set_primary":
            owner = getattr(self.project, msg["collection"]).get(msg["id"])
            if owner:
                gen = next(
                    (g for g in getattr(owner, "generations", [])
                     if g.id == msg["generation_id"] and g.file),
                    None,
                )
                if gen:
                    owner.image = gen.file
                    self.project.save()
                    await self.send_project()

        elif t == "delete_generation":
            owner = getattr(self.project, msg["collection"]).get(msg["id"])
            if owner and hasattr(owner, "generations"):
                owner.generations = [g for g in owner.generations if g.id != msg["generation_id"]]
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

    async def request_approval(self, kind: str, data: dict) -> dict:
        """Ask user to approve/edit/reject. Returns {action, data?, reason?}."""
        self._pending_approval = asyncio.get_event_loop().create_future()
        await self.send_event("approval_request", kind=kind, data=data)
        result = await self._pending_approval
        self._pending_approval = None
        return result

    async def edit(self, collection: str, fields: dict, approval: bool = False) -> dict:
        """Create (no id) or update (with id) entity. If approval=True — ask user first."""
        fields = {k: v for k, v in fields.items() if k != "self"}
        id = fields.pop("id", "")
        if approval:
            if id:
                obj = getattr(self.project, collection).get(id)
                if obj:
                    fields = {k: v or getattr(obj, k, "") for k, v in fields.items()}
            result = await self.request_approval(collection.rstrip("s"), fields)
            if result.get("action") == "reject":
                return {"status": "rejected", "reason": result.get("reason", "")}
            fields = result.get("data", {})

        if id:
            self.project.update(collection, id, **fields)
            status = "updated"
        else:
            obj = self.project.create(collection, **fields)
            id = obj.id
            status = "created"
        await self.send_project()
        return {"status": status, "id": id}

    async def send_event(self, event: str, **kwargs):
        if self.ws:
            await self.ws.send_text(json.dumps({"type": event, **kwargs}))

    def on_chat(self, callback):
        self._on_chat.append(callback)

    def on_tab_changed(self, callback):
        self._on_tab_changed.append(callback)

    async def start(self):
        import uvicorn
        config = uvicorn.Config(
            self.app, host="0.0.0.0", port=self.port, log_level="warning")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None

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
