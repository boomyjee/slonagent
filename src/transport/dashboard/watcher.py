"""WS-driven file watcher pool.

Watchers are tied to live WS subscriptions, not to server lifetime. Each
WS holds at most one active subscription — `subscribe(root, ws)` switches
it, `remove_client(ws)` clears it (called on disconnect). When the last
subscriber of a watcher leaves, the watcher cancels — no idle daemons.

Two clients on the same root share a watcher (cached by host path).
"""
import asyncio
from watchfiles import awatch, Change
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class _Watcher:
    def __init__(self, root: str, on_empty):
        self.root = root
        self.subs: set = set()
        self._task: asyncio.Task | None = None
        self._on_empty = on_empty

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def add(self, ws):
        self.subs.add(ws)

    def remove(self, ws):
        self.subs.discard(ws)
        if not self.subs:
            if self._task:
                self._task.cancel()
                self._task = None
            self._on_empty(self.root)

    async def _run(self):
        host_base = Path(self.root)
        try:
            async for changes in awatch(self.root):
                paths = []
                tree = False
                for ct, p in changes:
                    try:
                        rel = "/" + Path(p).relative_to(host_base).as_posix()
                    except ValueError:
                        continue
                    paths.append(rel)
                    if ct in (Change.added, Change.deleted):
                        tree = True
                if not paths:
                    continue
                payload = json.dumps({
                    "type": "files_changed",
                    "root": self.root,
                    "paths": paths,
                    "tree": tree,
                }, ensure_ascii=False)
                dead = []
                for ws in list(self.subs):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    self.remove(ws)
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("[watcher] %s crashed", self.root, exc_info=True)


class WatcherPool:
    def __init__(self, files):
        self._watchers: dict[str, _Watcher] = {}
        self._ws_subscription: dict = {}   # ws -> root
        self._files = files

    async def ws_handle_message(self, msg: dict, ws):
        if msg.get("type") != "watch":
            return
        root = msg.get("root") or self._files.default_root() or ""
        self._switch(ws, root)

    def on_ws_close(self, ws):
        self._switch(ws, "")

    def _switch(self, ws, new_root: str):
        old = self._ws_subscription.get(ws)
        if old == new_root:
            return
        if old:
            w = self._watchers.get(old)
            if w:
                w.remove(ws)
        if new_root:
            w = self._watchers.get(new_root)
            if w is None:
                w = _Watcher(new_root, on_empty=self._watchers.pop)
                self._watchers[new_root] = w
                w.start()
            w.add(ws)
            self._ws_subscription[ws] = new_root
        else:
            self._ws_subscription.pop(ws, None)
