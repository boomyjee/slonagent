"""WebSocket + HTTP server for movie creator mode."""
import asyncio
import base64
import json
import logging
import os
import webbrowser
from pathlib import Path

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse

from src.modes.movie_creator.project import Project

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class MovieServer:
    def __init__(self, port: int, project: Project, api_key: str = ""):
        self.port = port
        self.project = project
        self.api_key = api_key
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
            await self.send_project()
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
            await self.send_project()

        elif t in ("update_scene", "update_character"):
            collection = "scenes" if "scene" in t else "characters"
            self.project.update(collection, msg["id"], **msg.get("data", {}))
            await self.send_project()

        elif t in ("delete_scene", "delete_character"):
            collection = "scenes" if "scene" in t else "characters"
            self.project.delete(collection, msg["id"])
            await self.send_project()

        elif t == "reorder_scenes":
            self.project.reorder("scenes", msg.get("order", []))
            await self.send_project()

        elif t == "generate_portrait":
            asyncio.create_task(self.generate_portrait(msg["id"]))

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

    async def send_event(self, event: str, **kwargs):
        if self.ws:
            await self.ws.send_text(json.dumps({"type": event, **kwargs}))

    async def generate_portrait(self, char_id: str) -> dict:
        """Generate character portrait via Gemini Image API."""
        char = self.project.characters.get(char_id)
        if not char:
            return {"error": f"Character {char_id} not found"}
        if not char.appearance:
            return {"error": "No appearance description"}

        await self.send_event("generating", id=char_id, asset="portrait")

        prompt = (
            f"Cinematic portrait of a film character: {char.name}. "
            f"{char.appearance}. "
            "Head and shoulders, cinematic lighting, film still, shallow depth of field."
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash-image:generateContent?key={self.api_key}"
        )
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        proxies = {"http": proxy, "https": proxy} if proxy else None

        try:
            resp = await asyncio.to_thread(
                requests.post, url,
                json={"contents": [{"parts": [{"text": prompt}]}]},
                proxies=proxies, timeout=300,
            )
            if resp.status_code != 200:
                return {"error": f"API {resp.status_code}: {resp.text[:200]}"}

            for cand in resp.json().get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    if "inlineData" in part:
                        img = base64.b64decode(part["inlineData"]["data"])
                        filename = f"char_{char_id}.png"
                        self.project.assets_dir.mkdir(parents=True, exist_ok=True)
                        (self.project.assets_dir / filename).write_bytes(img)
                        self.project.update("characters", char_id, image=filename)
                        await self.send_project()
                        return {"status": "success", "image": filename}

            return {"error": "No image in API response"}
        except Exception as e:
            log.exception("[movie] portrait generation failed")
            return {"error": str(e)}

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
