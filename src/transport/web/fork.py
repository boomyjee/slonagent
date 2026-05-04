"""WebFork: один на agent.id. Держит HTTP-роуты, ws-клиентов, replay-буферы,
загруженные файлы. Несколько WebTransport-ов одного форка (main + thread
агенты) делят форк через WebTransport._forks с refcount."""

import asyncio, base64, glob, heapq, inspect, json, logging, mimetypes, os, re, shutil, time, uuid
from collections import deque
from pathlib import Path

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from src.transport.web.server import WebTransportServer

log = logging.getLogger(__name__)

_TEXT_EXT_RE = re.compile(
    r"\.(txt|md|py|js|ts|tsx|jsx|json|yaml|yml|xml|html|css|scss|sh|bash|sql|csv|log|toml|ini|conf|cfg|env)$",
    re.I,
)
_WEB_FILE_RE = re.compile(r"^WEB(?:_(.+))?\.json$")
_HISTORY_TAIL = 50

class WebFork:
    """Per-agent.id fork: shared HTTP routes, ws clients, replay buffers,
    upload pipeline. WebTransport-ы конкретных тред-агентов делят форк
    через WebTransport._forks с refcount."""

    # Class-level knobs subclasses override.
    prefix: str = ""           # DashboardFork.prefix = "/dashboard"
    mount: bool = True         # PageFork и т.п. с pre-accepted ws ставят False

    _MIME = {"js": "application/javascript", "css": "text/css", "html": "text/html",
             "json": "application/json", "svg": "image/svg+xml"}
    _STATIC_HEADERS = {
        "Cache-Control": "no-store",
        # Needed when a bookmarklet loads run.js cross-origin into a third-party page.
        "Access-Control-Allow-Origin": "*",
    }
    _BASE_UI = Path(__file__).resolve().parent / "ui"

    def __init__(self, ref_agent):
        # ref_agent — "представитель" агента форка (обычно main, thread_id="").
        # Используется для shared-ресурсов (memory_dir, thread_list, skills,
        # describe_video/transcribe_audio). НЕ зови на нём ничего, что
        # привязано к конкретному треду — для этого роутится через make_agent.
        self.ref_agent = ref_agent
        self.refcount = 0
        self.clients: set = set()
        self.transports: dict = {}
        self.last_active_ws: WebSocket | None = None
        # Replay-буферы — для catch-up на ws-реконнекте (юзер был дисконнект'нут).
        self.replay_transport: deque = deque(maxlen=100)
        self.replay_other: deque = deque(maxlen=400)
        # Монотон от epoch-ms — переживает рестарты, новые id всегда > старых.
        self.message_id_counter = int(time.time() * 1000)
        self._routes: list = []
        if self.mount:
            self.register_routes()

    def register_route(self, method, path, handler, auth: bool = True):
        url = f"/{self.ref_agent.id}{self.prefix}{path}"
        self._routes.append(WebTransportServer.register_route(method, url, handler, auth=auth))

    def register_json_route(self, method, path, handler):
        """Register a handler with contract (query, body, path_params) -> dict|list."""
        async def wrapped(request: Request):
            body = None
            if request.method in ("POST", "PUT", "PATCH"):
                try: body = await request.json()
                except Exception: pass
            result = await handler(dict(request.query_params), body, dict(request.path_params))
            if isinstance(result, str):
                return PlainTextResponse(result)
            return JSONResponse(result)
        wrapped.__name__ = f"json_{handler.__name__ if hasattr(handler, '__name__') else id(handler)}"
        self.register_route(method, path, wrapped)

    def cleanup(self):
        for route in self._routes:
            WebTransportServer.remove_route(route)
        self._routes = []

    # ─── URL helpers ─────────────────────────────────────────────────────

    async def get_url(self, sub_path: str = "", force_localhost: bool = False,
                      host: str | None = None, scheme: str = "http") -> str:
        return await WebTransportServer.get_url(
            self._path(sub_path), force_localhost=force_localhost, host=host, scheme=scheme,
        )

    async def get_auth_url(self, sub_path: str = "") -> str:
        return await WebTransportServer.get_auth_url(self._path(sub_path))

    def _path(self, sub_path: str = "") -> str:
        return f"/{self.ref_agent.id}{self.prefix}{sub_path}"

    # ─── Static / API endpoints ──────────────────────────────────────────

    def register_routes(self):
        self.register_route("get", "/uploads/{filename:path}", self._serve_upload)
        self.register_route("get", "/api/commands", self._api_commands)
        self.register_route("get", "/api/history", self._api_history)
        self.register_route("get", "/api/thread_list", self._api_thread_list)
        self.register_route("get", "/{filename:path}", self._static)
        self.register_route("websocket", "/ws", self._ws)

    async def _serve_upload(self, filename: str):
        path = os.path.join(self.uploads_dir, filename)
        # Path traversal guard
        if not os.path.realpath(path).startswith(os.path.realpath(self.uploads_dir)):
            return PlainTextResponse("Not found", status_code=404)
        if not os.path.isfile(path):
            return PlainTextResponse("Not found", status_code=404)
        mime, _ = mimetypes.guess_type(path)
        return FileResponse(path, media_type=mime or "application/octet-stream")

    async def _api_history(self, thread_id: str = ""):
        return JSONResponse({"events": self._load_history_tail(thread_id)})

    async def _api_thread_list(self):
        return JSONResponse(self.ref_agent.thread_list())

    async def _api_commands(self):
        cmds = {
            cmd: desc
            for skill in self.ref_agent.skills
            for cmd, desc in skill.get_bypass_commands(standalone_only=True).items()
        }
        return JSONResponse(cmds)

    @property
    def _ui_dirs(self) -> list[Path]:
        """Directories to search for static files, most-specific first.

        Subclass's `ui/` (next to the subclass source file) is checked first;
        web/ui/ is the fallback with shared components (lib.js, Chat.js).
        Subclasses override individual files by placing them in their own
        `ui/` directory."""
        own = Path(inspect.getfile(type(self))).resolve().parent / "ui"
        dirs = []
        if own != self._BASE_UI and own.is_dir():
            dirs.append(own)
        dirs.append(self._BASE_UI)
        return dirs

    async def _static(self, filename: str = "index.html"):
        filename = filename or "index.html"
        for ui_dir in self._ui_dirs:
            path = ui_dir / filename
            if path.is_file() and path.resolve().is_relative_to(ui_dir.resolve()):
                mime = self._MIME.get(path.suffix.lstrip("."), "text/plain")
                return PlainTextResponse(
                    path.read_text(encoding="utf-8"),
                    media_type=mime,
                    headers=self._STATIC_HEADERS,
                )
        return PlainTextResponse("Not found", status_code=404)

    # ─── WebSocket endpoint ──────────────────────────────────────────────

    async def _ws(self, ws: WebSocket):
        await ws.accept()
        await self.ws_connect(ws)

    async def ws_connect(self, ws: WebSocket):
        # No auto-replay — clients ask for it explicitly via a "replay" message
        # after they connect (so reconnects on a live page can specify
        # last_seen_id and skip what they already have).
        # Реестр имён тредов — snapshot, отдаётся по GET /api/thread_list. Тут
        # его не флудим как фейковые thread_rename'ы.
        self.clients.add(ws)
        try:
            while True:
                data = await ws.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    log.warning("ws: invalid JSON: %s", data[:200])
                    continue
                await self.ws_handle_message(msg, ws)
        except WebSocketDisconnect:
            pass
        finally:
            self.clients.discard(ws)
            if self.last_active_ws is ws:
                self.last_active_ws = None
            self.on_ws_close(ws)

    def on_ws_close(self, ws):
        """Hook for subclasses to clean up per-connection state. Base no-op."""
        pass

    async def ws_handle_message(self, msg: dict, ws=None):
        if msg.get("type") == "replay" and ws is not None:
            last_seen = msg.get("last_seen_id", -1)
            stream = heapq.merge(self.replay_transport, self.replay_other,
                                 key=lambda e: e.get("id", 0))
            for event in stream:
                if event.get("id", 0) > last_seen:
                    await ws.send_text(json.dumps(event, ensure_ascii=False))
            return
        if msg.get("type") == "transport" and msg.get("method") == "process_message":
            # Запоминаем клиента который последним прислал user message — туда
            # уйдут адресные команды от агента (например, dashboard.open_tab).
            if ws is not None:
                self.last_active_ws = ws
            new_parts = []
            for p in msg.get("content_parts", []):
                if isinstance(p, dict) and p.get("type") == "file":
                    new_parts.extend(await self._process_file(p))
                else:
                    new_parts.append(p)
            msg = {**msg, "content_parts": new_parts}
            content_parts = new_parts

            # Echo back through send() so it lands in the buffer and gets
            # replayed on reconnect. Chat.js no longer adds user messages
            # to local state — it renders them when this event comes in.
            await self.send(msg, replay=True)

            try:
                tid = msg.get("thread_id", "")
                if tid not in self.transports:
                    from src.transport.web import WebTransport
                    await WebTransport.make_agent(self.ref_agent.id, tid)

                await self.transports[tid].process_message(
                    content_parts=content_parts,
                    user_message_id=msg.get("user_message_id"),
                    trigger_answer=msg.get("trigger_answer", True),
                )
            except Exception:
                log.exception("[ws] process_message FAILED",exc_info=True)
            return
        if msg.get("type") == "transport" and msg.get("method") == "thread_rename":
            uuid_, name = msg.get("uuid"), msg.get("name")
            if uuid_ is not None and name is not None:
                await self.ref_agent.thread_rename(uuid_, name)

    # ─── Outbound — fan-out to clients ───────────────────────────────────

    async def send(self, event: dict, replay=False):
        self.message_id_counter += 1
        event = {**event, "id": self.message_id_counter}
        if replay:
            buf = self.replay_transport if event.get("type") == "transport" else self.replay_other
            buf.append(event)
        # History на диске — параллельный механизм, для первичной загрузки таба
        # через GET /api/history. Не stream-partial, не processing.
        if (event.get("type") == "transport"
                and event.get("final") is not False
                and event.get("method") != "send_processing"):
            self._append_history(event)
        if not self.clients: return
        data = json.dumps(event, ensure_ascii=False)
        dead = set()
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    # ─── Persisted transport history (WEB_<thread_id>.json) ─────────────────

    def _history_path(self, thread_id: str) -> str:
        fname = f"WEB_{thread_id}.json" if thread_id else "WEB.json"
        return os.path.join(self.ref_agent.memory.memory_dir, fname)

    def _append_history(self, event: dict):
        path = self._history_path(event.get("thread_id", ""))
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("[fork] persist %s failed: %s", path, e, exc_info=True)

    def _load_history_tail(self, thread_id: str) -> list[dict]:
        """Последние _HISTORY_TAIL событий из WEB_<thread_id>.json."""
        path = self._history_path(thread_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()[-_HISTORY_TAIL:]
            return [json.loads(line) for line in lines if line.strip()]
        except Exception as e:
            log.warning("[fork] load %s failed: %s", path, e, exc_info=True)
            return []

    async def send_targeted(self, event: dict):
        """Шлёт только клиенту, который последним прислал user-message.
        Если такого нет (никто не подключён или disconnected) — кидает,
        чтобы вызывающий tool вернул ошибку в ллм."""
        target = self.last_active_ws
        if target is None or target not in self.clients:
            raise RuntimeError("no active client")
        self.message_id_counter += 1
        event = {**event, "id": self.message_id_counter}
        try:
            await target.send_text(json.dumps(event, ensure_ascii=False))
        except Exception as e:
            self.clients.discard(target)
            self.last_active_ws = None
            raise RuntimeError(f"target client send failed: {e}")

    # ─── Uploads (inbound + outbound) ────────────────────────────────────

    @property
    def uploads_dir(self) -> str:
        """<memory_dir>/uploads/ — место хранения присланных юзером файлов.
        Внутри memory/, а значит под .gitignore."""
        d = os.path.join(self.ref_agent.memory.memory_dir, "uploads")
        os.makedirs(d, exist_ok=True)
        return d

    async def _save_upload(self, data: bytes, ext: str) -> str:
        file_id = uuid.uuid4().hex
        out_path = os.path.join(self.uploads_dir, f"{file_id}{ext}")
        await asyncio.to_thread(self._save_with_gc, out_path, data)
        return file_id

    @staticmethod
    def _attached_file_part(file_id: str, filename: str, field: str, mime: str, size: int,
                            content: str | None) -> dict:
        attrs = (f'file_id="{file_id}" filename="{filename}" type="{field}" '
                 f'mime_type="{mime}" size_bytes="{size}"')
        if content:
            text = f"<attached_file {attrs}>\n<content>\n{content}\n</content>\n</attached_file>"
        else:
            text = f"<attached_file {attrs} />"
        part = {"type": "text", "text": text}
        if content and field == "document":
            part["_document_id"] = f"{file_id}_{filename}"
        return part

    async def _process_file(self, p: dict) -> list[dict]:
        """Единственный диспатч аттача: сохраняет байты, возвращает 1+
        content_part'ов в зависимости от mime — image/* отдаёт также
        image_url для vision, voice/video транскрибируются/описываются,
        текстовые инлайнятся в <content>, остальное — self-closing мета."""
        filename = p.get("name") or "file"
        mime = p.get("mime") or "application/octet-stream"
        size = p.get("size") or 0
        try:
            data = base64.b64decode(p.get("data", ""))
        except Exception as e:
            return [{"type": "text", "text": f"⚠️ Не удалось декодировать файл {filename}: {e}"}]
        ext = os.path.splitext(filename)[1].lower()[:16] or (mimetypes.guess_extension(mime) or "")
        try:
            file_id = await self._save_upload(data, ext)
        except Exception as e:
            log.exception("file save failed")
            return [{"type": "text", "text": f"⚠️ Не удалось сохранить файл {filename}: {e}"}]

        content = None
        field = "document"
        extra: list[dict] = []
        if mime.startswith("image/"):
            field = "photo"
            extra.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{p['data']}"}})
        elif mime.startswith("video/"):
            field = "video"
            try:
                content = await self.ref_agent.describe_video(data, mime)
            except Exception:
                log.exception("video describe failed")
        elif mime.startswith("audio/"):
            field = "voice"
            try:
                content = await self.ref_agent.transcribe_audio(data, mime)
            except Exception as e:
                log.exception("voice transcription failed")
                content = f"⚠️ Не удалось распознать: {e}"
        elif (mime.startswith("text/") or _TEXT_EXT_RE.search(filename)) and size < 1_048_576:
            try:
                content = data.decode("utf-8", errors="replace")
            except Exception:
                log.exception("text file inline failed")
        return extra + [self._attached_file_part(file_id, filename, field, mime, size, content)]

    def _save_with_gc(self, out_path: str, data: bytes):
        """Пишет файл и заодно прогоняет GC через _gc_uploads — replay-aware,
        не удалит outbound-файлы на которые ещё есть ссылки в буфере событий."""
        with open(out_path, "wb") as f:
            f.write(data)
        self._gc_uploads()

    def publish_file(self, host_path: str, compress_image: bool = False) -> dict:
        """Копирует файл в uploads_dir с уникальным именем, возвращает dict
        с метаданными для WS-события. URL относительный — сам сервер где
        смонтирован, такой и будет работать (./uploads/...).

        compress_image=True жмёт картинку до 1920×1920 JPEG q85. Симметрично
        компрессии на инбаунде (Chat.js compressImage)."""
        token = uuid.uuid4().hex
        if compress_image:
            try:
                from PIL import Image
                img = Image.open(host_path)
                img.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                out_name = f"{token}.jpg"
                out_path = os.path.join(self.uploads_dir, out_name)
                img.save(out_path, format="JPEG", quality=85, optimize=True)
                display_name = os.path.splitext(os.path.basename(host_path))[0] + ".jpg"
                mime = "image/jpeg"
            except Exception as e:
                log.warning("[publish] image compress failed for %s: %s — sending as-is", host_path, e)
                compress_image = False
        if not compress_image:
            ext = os.path.splitext(host_path)[1]
            out_name = f"{token}{ext}"
            out_path = os.path.join(self.uploads_dir, out_name)
            shutil.copyfile(host_path, out_path)
            display_name = os.path.basename(host_path)
            mime, _ = mimetypes.guess_type(host_path)
            mime = mime or "application/octet-stream"
        result = {
            "url": f"./uploads/{out_name}",
            "name": display_name,
            "size": os.path.getsize(out_path),
            "mime": mime,
        }
        self._gc_uploads()
        return result

    def _gc_uploads(self):
        """Удаляет файлы старше суток, на которые нет ссылок в replay-буферах
        или в файлах истории. Если событие с URL ещё кто-то отдаст клиенту —
        файл должен быть на месте."""
        cutoff = time.time() - 24 * 3600
        referenced: set[str] = set()
        for buf in (self.replay_transport, self.replay_other):
            for ev in buf:
                self._collect_upload_names(ev, referenced)
        # Tail из всех history-файлов треда — то что отдаём через /api/history.
        pattern = os.path.join(self.ref_agent.memory.memory_dir, "WEB*.json")
        for path in glob.glob(pattern):
            m = _WEB_FILE_RE.match(os.path.basename(path))
            if not m:
                continue
            for ev in self._load_history_tail(m.group(1) or ""):
                self._collect_upload_names(ev, referenced)
        for entry in os.scandir(self.uploads_dir):
            try:
                if not entry.is_file(): continue
                if entry.stat().st_mtime >= cutoff: continue
                if entry.name in referenced: continue
                os.remove(entry.path)
            except OSError:
                pass

    @staticmethod
    def _collect_upload_names(obj, out: set):
        """Рекурсивно собирает basename'ы файлов uploads из URL'ов вида './uploads/<name>'."""
        if isinstance(obj, dict):
            url = obj.get("url")
            if isinstance(url, str) and "/uploads/" in url:
                out.add(url.rsplit("/uploads/", 1)[1].split("?", 1)[0].split("#", 1)[0])
            for v in obj.values():
                WebFork._collect_upload_names(v, out)
        elif isinstance(obj, list):
            for v in obj:
                WebFork._collect_upload_names(v, out)
