"""HTTP + WebSocket server for coding mode."""
import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class CodingServer:
    def __init__(self, port: int, resolve_path_fn, root_path: str = "/workspace"):
        self.port = port
        self.resolve_path = resolve_path_fn
        self.root_path = root_path
        self.app = FastAPI()
        self.ws: WebSocket | None = None
        self.chat_event = asyncio.Event()
        self.last_chat: str | None = None
        self._setup_routes()

    def _setup_routes(self):
        @self.app.get("/")
        async def index():
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            html = html.replace("__ROOT_PATH__", self.root_path)
            return HTMLResponse(html)

        @self.app.get("/api/files")
        async def list_files(path: str = Query("/")):
            host_path = self.resolve_path(path)
            if host_path is None:
                return JSONResponse({"error": f"Access denied: {path}"}, 403)
            if not os.path.isdir(host_path):
                return JSONResponse({"error": f"Not a directory: {path}"}, 400)
            entries = []
            for name in sorted(os.listdir(host_path)):
                if name.startswith("."):
                    continue
                full = os.path.join(host_path, name)
                entries.append({
                    "name": name,
                    "is_dir": os.path.isdir(full),
                    "path": (path.rstrip("/") + "/" + name),
                })
            return JSONResponse({"entries": entries})

        @self.app.get("/api/file")
        async def read_file(path: str = Query(...)):
            host_path = self.resolve_path(path)
            if host_path is None:
                return JSONResponse({"error": f"Access denied: {path}"}, 403)
            if not os.path.isfile(host_path):
                return JSONResponse({"error": f"Not a file: {path}"}, 400)
            try:
                with open(host_path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                return JSONResponse({"path": path, "content": content})
            except Exception as e:
                return JSONResponse({"error": str(e)}, 500)

        @self.app.put("/api/file")
        async def write_file(request):
            data = await request.json()
            path, content = data.get("path"), data.get("content")
            host_path = self.resolve_path(path)
            if host_path is None:
                return JSONResponse({"error": f"Access denied: {path}"}, 403)
            try:
                os.makedirs(os.path.dirname(host_path), exist_ok=True)
                with open(host_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return JSONResponse({"status": "ok"})
            except Exception as e:
                return JSONResponse({"error": str(e)}, 500)

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await ws.accept()
            self.ws = ws
            try:
                while True:
                    data = await ws.receive_text()
                    msg = json.loads(data)
                    if msg["type"] == "chat":
                        self.last_chat = msg["text"]
                        self.chat_event.set()
            except WebSocketDisconnect:
                self.ws = None

    async def wait_for_chat(self) -> str:
        self.chat_event.clear()
        self.last_chat = None
        await self.chat_event.wait()
        return self.last_chat

    async def send_chat(self, text: str, role: str = "assistant"):
        if self.ws:
            await self.ws.send_text(json.dumps({"type": "message", "role": role, "text": text}))

    async def send_tool_call(self, name: str, args: dict):
        if self.ws:
            await self.ws.send_text(json.dumps({"type": "tool_call", "name": name, "args": args}))

    async def send_event(self, event: str):
        if self.ws:
            await self.ws.send_text(json.dumps({"type": event}))

    async def start(self):
        import uvicorn
        config = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning")
        server = uvicorn.Server(config)

        async def _serve():
            try:
                await server.serve()
            except SystemExit:
                pass

        asyncio.create_task(_serve())
        for _ in range(50):
            if server.started:
                log.info("[coding] server started on port %d", self.port)
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Failed to start server on port {self.port}")
