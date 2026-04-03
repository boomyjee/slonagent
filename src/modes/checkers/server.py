"""HTTP + WebSocket server for checkers game."""
import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from src.modes.checkers.game import Board, ai_move, owner

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class CheckersServer:
    def __init__(self, port: int = 3100):
        self.port = port
        self.app = FastAPI()
        self.board = Board()
        self.ws: WebSocket | None = None
        self.move_event = asyncio.Event()
        self.last_user_move: dict | None = None
        self.game_over = False
        self._setup_routes()

    def _setup_routes(self):
        @self.app.get("/")
        async def index():
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(html)

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await ws.accept()
            self.ws = ws
            await self._send_state()
            try:
                while True:
                    data = await ws.receive_text()
                    msg = json.loads(data)
                    if msg["type"] == "move":
                        self.last_user_move = msg
                        self.move_event.set()
            except WebSocketDisconnect:
                self.ws = None

    async def _send_state(self, last_move=None, comment=None, your_turn=True):
        if not self.ws:
            return
        valid_moves = {}
        if not self.game_over and your_turn:
            raw = self.board.get_all_moves(1)
            valid_moves = {f"{r},{c}": chains for (r, c), chains in raw.items()}
        msg = {
            "type": "state",
            "board": self.board.to_list(),
            "valid_moves": valid_moves,
            "game_over": self.game_over,
        }
        if last_move:
            msg["last_move"] = last_move
        if comment:
            msg["comment"] = comment
        await self.ws.send_text(json.dumps(msg))

    async def send_comment(self, text: str):
        if self.ws:
            await self.ws.send_text(json.dumps({"type": "comment", "text": text}))

    async def wait_for_user_move(self) -> dict:
        self.move_event.clear()
        self.last_user_move = None
        await self.move_event.wait()
        return self.last_user_move

    def reset(self):
        self.board = Board()
        self.game_over = False

    async def start(self):
        import uvicorn
        config = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning")
        server = uvicorn.Server(config)
        self._server = server

        async def _serve():
            try:
                await server.serve()
            except SystemExit:
                pass

        asyncio.create_task(_serve())
        for _ in range(50):
            if server.started:
                log.info("[checkers] server started on port %d", self.port)
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Failed to start server on port {self.port}")
