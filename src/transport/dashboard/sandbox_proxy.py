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
import asyncio, base64, itertools, json, logging, shlex, subprocess

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

log = logging.getLogger(__name__)


class SandboxProxy:
    START_TIMEOUT = 30.0
    HTTP_TIMEOUT = 120.0

    def __init__(self, transport):
        self.transport = transport
        self.tunnel: WebSocket | None = None
        self.tunnel_ready = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending_http: dict[int, asyncio.Future] = {}
        self._ws_sessions: dict[int, asyncio.Queue] = {}
        self._start_lock = asyncio.Lock()
        self._host_ip: str | None = None

    async def handle_tunnel(self, ws: WebSocket):
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
        sandbox = self.transport._sandbox
        if sandbox is None:
            return False
        url = await self._worker_url()
        # Always respawn: the URL (host IP, agent id) can change between
        # slonagent restarts, and an already-running worker may be stuck in
        # a reconnect loop against a stale URL.
        cmd = (
            "if [ -f /tmp/sandbox_proxy.pid ]; then "
            "kill $(cat /tmp/sandbox_proxy.pid) 2>/dev/null; fi; "
            "nohup python3 -u /slonagent/sandbox_proxy.py "
            f"--url {shlex.quote(url)} > /tmp/sandbox_proxy.log 2>&1 & "
            "echo $! > /tmp/sandbox_proxy.pid"
        )
        log.info("[sandbox-proxy] starting worker for %s", url)
        await sandbox.exec(cmd, timeout=10)
        return True

    async def _worker_url(self) -> str:
        from src.transport.web import WebTransport
        return (f"ws://{await self._host_address()}:{WebTransport._port}"
                f"/{self.transport.agent.id}/dashboard/sandbox-tunnel")

    async def _host_address(self) -> str:
        """Hostname/IP the container can use to reach the host's uvicorn.

        Docker Desktop wires up `host.docker.internal` transparently, so we
        don't need anything fancy there. Podman is tricker: on Win/Mac it
        runs inside a `podman machine` VM, and inside the agent's container
        `host.containers.internal` isn't populated (--no-hosts) and the
        slirp gateway (10.0.2.2) points at the VM's loopback, not the real
        host. We fetch the VM's default gateway (= the host from the VM's
        view) via asyncssh using the machine's own SSH config. On bare
        Linux podman there's no machine, so we fall through to the magic
        alias which podman auto-populates in /etc/hosts.
        """
        if self._host_ip:
            return self._host_ip
        sandbox = self.transport._sandbox
        runtime = sandbox.runtime if sandbox else "podman"
        if runtime == "podman":
            ip = await self._podman_machine_gateway()
            if ip:
                self._host_ip = ip
                log.info("[sandbox-proxy] host ip via podman-machine: %s", ip)
                return ip
            self._host_ip = "host.containers.internal"
        else:
            self._host_ip = "host.docker.internal"
        log.info("[sandbox-proxy] host ip (%s): %s", runtime, self._host_ip)
        return self._host_ip

    async def _podman_machine_gateway(self) -> str | None:
        try:
            import asyncssh
            out = await asyncio.to_thread(
                subprocess.run, ["podman", "machine", "inspect"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode != 0:
                raise RuntimeError(f"rc={out.returncode}: {out.stderr!r}")
            machines = json.loads(out.stdout)
            if not machines:
                return None
            ssh = machines[0]["SSHConfig"]
            key = asyncssh.read_private_key(ssh["IdentityPath"])
            async with asyncssh.connect(
                "127.0.0.1", port=ssh["Port"], username=ssh["RemoteUsername"],
                client_keys=[key], known_hosts=None, connect_timeout=10,
            ) as conn:
                result = await conn.run("ip route show default", check=False)
            parts = (result.stdout or "").split()
            if "via" in parts:
                return parts[parts.index("via") + 1]
            log.warning("[sandbox-proxy] unexpected ip route output: %r", result.stdout)
        except Exception as e:
            log.warning("[sandbox-proxy] podman-machine gateway lookup failed: %r", e)
        return None
