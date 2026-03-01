import asyncio
import json
import logging
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
_BUFFER_SIZE = 500


class Dashboard:
    def __init__(self, port: int = 8765):
        self._port = port
        self._queue: asyncio.Queue = None
        self._clients: set = set()
        self._buffer: deque = deque(maxlen=_BUFFER_SIZE)

    def call_later(self, fn, *args):
        fn(*args)

    def _emit(self, event: dict) -> None:
        event["ts"] = datetime.now().strftime("%H:%M:%S")
        self._buffer.append(event)
        if self._queue is not None:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def add_chat(self, role: str, text: str) -> None:
        self._emit({"type": "chat", "role": role, "text": text})

    def add_collapsible(self, title: str, text: str) -> None:
        self._emit({"type": "collapsible", "title": title, "text": text})

    def add_log(self, category: str, level: str, text: str) -> None:
        self._emit({"type": "log", "category": category, "level": level, "text": text})

    async def _broadcaster(self) -> None:
        while True:
            event = await self._queue.get()
            if not self._clients:
                continue
            data = json.dumps(event, ensure_ascii=False)
            dead = set()
            for client in list(self._clients):
                try:
                    await client.send_text(data)
                except Exception:
                    dead.add(client)
            self._clients -= dead

    async def run_async(self) -> None:
        import uvicorn

        self._queue = asyncio.Queue(maxsize=2000)
        asyncio.create_task(self._broadcaster())

        app = FastAPI()

        @app.get("/")
        async def index():
            return HTMLResponse(_HTML)

        @app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()
            for event in self._buffer:
                await websocket.send_text(json.dumps(event, ensure_ascii=False))
            self._clients.add(websocket)
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        async def open_browser():
            await asyncio.sleep(1.0)
            webbrowser.open(f"http://localhost:{self._port}")

        asyncio.create_task(open_browser())

        config = uvicorn.Config(app, host="0.0.0.0", port=self._port, log_level="info", ws="wsproto")
        server = uvicorn.Server(config)
        await server.serve()


class UILogHandler(logging.Handler):
    def __init__(self, dashboard: Dashboard, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self._dashboard = dashboard

    def _category(self, name: str) -> str:
        if name.startswith("src.memory") or name.startswith("memory"):
            return "memory"
        if name.startswith("aiogram") or name.startswith("src.transport") or name.startswith("httpx"):
            return "transport"
        return "agent"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            category = self._category(record.name)
            text = self.format(record)
            if " - " in text:
                text = text.split(" - ", 3)[-1]
            self._dashboard.add_log(category, record.levelname, text)
        except Exception:
            self.handleError(record)
