"""WebSocket + HTTP server for movie creator mode."""
import asyncio
import json
import logging
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
        self.chat_queue: asyncio.Queue[str] = asyncio.Queue()
        self.active_tab: str = "screenplay"
        self._on_tab_changed: list = []  # callbacks
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

        if t == "create_scene":
            d = msg.get("data", {})
            self.project.create_scene(
                title=d.get("title", ""),
                text=d.get("text", ""),
                location=d.get("location", ""),
            )
            await self._send_project()

        elif t == "update_scene":
            self.project.update_scene(msg["id"], **msg.get("data", {}))
            await self._send_project()

        elif t == "delete_scene":
            self.project.delete_scene(msg["id"])
            await self._send_project()

        elif t == "reorder_scenes":
            self.project.reorder_scenes(msg.get("order", []))
            await self._send_project()

        elif t == "chat":
            await self.chat_queue.put(msg["text"])

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

    async def send_chat(self, text: str, role: str = "assistant"):
        if self.ws:
            await self.ws.send_text(json.dumps({
                "type": "message", "role": role, "text": text,
            }))

    async def send_scene_proposal(self, data: dict):
        """AI proposes a scene — client shows edit modal."""
        if self.ws:
            await self.ws.send_text(json.dumps({
                "type": "scene_proposal", "data": data,
            }))

    async def send_event(self, event: str, **kwargs):
        if self.ws:
            await self.ws.send_text(json.dumps({"type": event, **kwargs}))

    async def wait_for_chat(self) -> str:
        return await self.chat_queue.get()

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
