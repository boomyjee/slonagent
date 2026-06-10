"""Reverse-tunnel proxy: forwards `/dashboard/sandbox/{port}/...` requests to
ports bound on 127.0.0.1 inside the sandbox container.

Podman-machine containers don't publish ports, so the host can't reach them
directly. Instead, a worker runs inside the container, opens a single control
WebSocket to the host (/dashboard/sandbox-tunnel) via `host.containers.internal`,
and we multiplex HTTP/WS requests over it. See container_lib/sandbox_proxy.py
for the frame protocol.

The worker is started lazily on the first /sandbox/{port}/... request through
`SandboxSkill.exec` with nohup+&, so the cold-start latency (pip install +
websocket handshake) is paid only once.
"""
import asyncio, base64, itertools, json, logging, secrets, shlex, subprocess

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

log = logging.getLogger(__name__)


class SandboxProxy:
    START_TIMEOUT = 30.0
    HTTP_TIMEOUT = 120.0

    def __init__(self, fork):
        self.fork = fork
        self.tunnel: WebSocket | None = None
        self.tunnel_ready = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending_http: dict[int, asyncio.Future] = {}
        self._ws_sessions: dict[int, asyncio.Queue] = {}
        self._start_lock = asyncio.Lock()
        # Секрет control-канала: воркер коннектится из контейнера без cookie,
        # поэтому /sandbox-tunnel гейтится этим токеном в URL, а не auth.
        self._token = secrets.token_urlsafe(32)

    async def handle_tunnel(self, ws: WebSocket, token: str):
        if not secrets.compare_digest(token, self._token):
            await ws.close(code=4401)
            return
        await ws.accept()
        if self.tunnel is not None:
            try: await self.tunnel.close()
            except Exception: pass
        self.tunnel = ws
        self.tunnel_ready.set()
        log.info("[sandbox-proxy] tunnel connected")
        try:
            while True:
                frame = json.loads(await ws.receive_text())
                self._route_from_worker(frame)
        except WebSocketDisconnect:
            pass
        finally:
            log.info("[sandbox-proxy] tunnel disconnected")
            if self.tunnel is ws:
                self.tunnel = None
                self.tunnel_ready.clear()
            for fut in self._pending_http.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("tunnel closed"))
            self._pending_http.clear()
            for q in self._ws_sessions.values():
                q.put_nowait(None)
            self._ws_sessions.clear()

    def _route_from_worker(self, frame):
        kind, id_ = frame["kind"], frame["id"]
        if kind in ("resp", "cgi_resp"):
            fut = self._pending_http.pop(id_, None)
            if fut and not fut.done():
                fut.set_result(frame)
        elif kind in ("ws_opened", "ws_fail", "ws_s2c", "ws_closed"):
            q = self._ws_sessions.get(id_)
            if q:
                q.put_nowait(frame)

    async def handle_cgi(self, filepath: str, request: Request):
        """Прогнать /workspace/web/<filepath> как Python-скрипт через worker
        в контейнере. Экономия cold-start'а — exec идёт в долгоживущем
        процессе sandbox_proxy.py, не через `podman exec`."""
        if not await self._ensure_tunnel():
            return Response("sandbox proxy unavailable", status_code=502)
        id_ = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending_http[id_] = fut
        body = await request.body()
        headers = {k.decode(): v.decode() for k, v in request.headers.raw if k.lower() != b"host"}
        await self._send({
            "id": id_, "kind": "cgi", "path": filepath,
            "method": request.method,
            "query": dict(request.query_params),
            "headers": headers,
            "cookies": dict(request.cookies),
            "body_b64": base64.b64encode(body).decode(),
        })
        try:
            frame = await asyncio.wait_for(fut, timeout=self.HTTP_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending_http.pop(id_, None)
            return Response("cgi timeout", status_code=504)
        except Exception as e:
            return Response(f"cgi error: {e}", status_code=502)
        skip = {"transfer-encoding", "content-encoding", "content-length", "connection"}
        resp_headers = {k: v for k, v in (frame.get("headers") or {}).items() if k.lower() not in skip}
        return Response(
            content=base64.b64decode(frame.get("body_b64") or ""),
            status_code=frame.get("status", 502),
            headers=resp_headers,
        )

    async def handle_http(self, port: int, filepath: str, request: Request):
        if not await self._ensure_tunnel():
            return Response("sandbox proxy unavailable", status_code=502)
        id_ = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending_http[id_] = fut
        body = await request.body()
        path = "/" + filepath + (f"?{request.url.query}" if request.url.query else "")
        headers = {k.decode(): v.decode() for k, v in request.headers.raw if k.lower() != b"host"}
        await self._send({
            "id": id_, "kind": "http", "port": port,
            "method": request.method, "path": path, "headers": headers,
            "body_b64": base64.b64encode(body).decode(),
        })
        try:
            frame = await asyncio.wait_for(fut, timeout=self.HTTP_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending_http.pop(id_, None)
            return Response("proxy timeout", status_code=504)
        except Exception as e:
            return Response(f"proxy error: {e}", status_code=502)
        # Drop hop-by-hop headers so FastAPI/uvicorn sets them correctly.
        skip = {"transfer-encoding", "content-encoding", "content-length", "connection"}
        resp_headers = {k: v for k, v in (frame.get("headers") or {}).items() if k.lower() not in skip}
        return Response(
            content=base64.b64decode(frame.get("body_b64") or ""),
            status_code=frame.get("status", 502),
            headers=resp_headers,
        )

    async def handle_ws(self, port: int, filepath: str, ws: WebSocket):
        await ws.accept()
        if not await self._ensure_tunnel():
            await ws.close(code=1011)
            return
        id_ = next(self._ids)
        q: asyncio.Queue = asyncio.Queue()
        self._ws_sessions[id_] = q
        path = "/" + filepath + (f"?{ws.url.query}" if ws.url.query else "")
        headers = {k.decode(): v.decode() for k, v in ws.headers.raw if k.lower() != b"host"}
        await self._send({
            "id": id_, "kind": "ws_open", "port": port,
            "path": path, "headers": headers,
        })
        first = await q.get()
        if first is None or first.get("kind") != "ws_opened":
            reason = (first or {}).get("reason", "tunnel closed")
            log.info("[sandbox-proxy] ws_open failed: %s", reason)
            self._ws_sessions.pop(id_, None)
            await ws.close(code=1011)
            return

        async def c2s():
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("text") is not None:
                        data, binary = msg["text"].encode(), False
                    else:
                        data, binary = msg.get("bytes") or b"", True
                    await self._send({
                        "id": id_, "kind": "ws_c2s",
                        "data_b64": base64.b64encode(data).decode(),
                        "binary": binary,
                    })
            finally:
                await self._send({"id": id_, "kind": "ws_close"})

        async def s2c():
            while True:
                frame = await q.get()
                if frame is None:
                    return
                if frame.get("kind") == "ws_s2c":
                    data = base64.b64decode(frame.get("data_b64") or "")
                    if frame.get("binary"):
                        await ws.send_bytes(data)
                    else:
                        await ws.send_text(data.decode())
                elif frame.get("kind") == "ws_closed":
                    return

        try:
            await asyncio.gather(c2s(), s2c(), return_exceptions=True)
        finally:
            self._ws_sessions.pop(id_, None)
            try: await ws.close()
            except Exception: pass

    async def _send(self, frame):
        if self.tunnel:
            try:
                await self.tunnel.send_text(json.dumps(frame))
            except Exception as e:
                log.warning("[sandbox-proxy] send failed: %s", e)

    async def _ensure_tunnel(self) -> bool:
        if self.tunnel is not None:
            return True
        async with self._start_lock:
            if self.tunnel is not None:
                return True
            if not await self._start_worker():
                return False
            try:
                await asyncio.wait_for(self.tunnel_ready.wait(), timeout=self.START_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("[sandbox-proxy] worker didn't connect within %ss", self.START_TIMEOUT)
                return False
        return self.tunnel is not None

    async def _start_worker(self) -> bool:
        sandbox = self.fork.ref_agent.sandbox
        if sandbox is None:
            return False
        url = await self._worker_url()
        # Kill old worker by pattern, but skip processes whose cmdline
        # contains "pkill" (i.e. the bash wrapper running this very command).
        # Plain `pkill -f` would match the parent shell's cmdline too.
        cmd = (
            "for p in $(pgrep -f sandbox_proxy.py); do "
            "grep -qz pkill /proc/$p/cmdline 2>/dev/null || kill $p 2>/dev/null; "
            "done; sleep 0.5; "
            "nohup python3 -u /slonagent/sandbox_proxy.py "
            f"--url {shlex.quote(url)} > /tmp/sandbox_proxy.log 2>&1 & "
            "echo $! > /tmp/sandbox_proxy.pid"
        )
        log.info("[sandbox-proxy] starting worker for %s", url)
        await sandbox.exec(cmd, timeout=10)
        return True

    async def _worker_url(self) -> str:
        from src.skills.sandbox import SandboxSkill
        return await self.fork.get_url(
            f"/sandbox-tunnel/{self._token}",
            host=await SandboxSkill.host_url(),
            scheme="ws",
        )
