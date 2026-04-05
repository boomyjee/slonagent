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
        self.app = FastAPI()
        self.ws: WebSocket | None = None
        self.active_tab: str = "screenplay"
        self._on_chat: list = []  # callbacks
        self._on_tab_changed: list = []  # callbacks
        self._pending_approval: asyncio.Future | None = None
        self._setup_routes()

    def _setup_routes(self):
        @self.app.get("/")
        async def index():
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(html)

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
            await self._send_project()
            try:
                while True:
                    data = await ws.receive_text()
                    await self._handle_message(json.loads(data))
            except WebSocketDisconnect:
                self.ws = None

    async def _handle_message(self, msg: dict):
        t = msg.get("type")

        if t in ("create_scene", "create_character"):
            collection = "scenes" if "scene" in t else "characters"
            self.project.create(collection, **msg.get("data", {}))
            await self._send_project()

        elif t in ("update_scene", "update_character"):
            collection = "scenes" if "scene" in t else "characters"
            self.project.update(collection, msg["id"], **msg.get("data", {}))
            await self._send_project()

        elif t in ("delete_scene", "delete_character"):
            collection = "scenes" if "scene" in t else "characters"
            self.project.delete(collection, msg["id"])
            await self._send_project()

        elif t == "reorder_scenes":
            self.project.reorder("scenes", msg.get("order", []))
            await self._send_project()

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

    async def _send_project(self):
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
