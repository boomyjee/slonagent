"""Reverse-tunnel worker: runs inside the sandbox container, proxies HTTP/WS
traffic from the host's dashboard to ports bound on 127.0.0.1 in the container.

Started lazily by the host via:
    nohup python3 -u /slonagent/sandbox_proxy.py --url ws://host.containers.internal:PORT/AID/dashboard/sandbox-tunnel &

Protocol — JSON text frames over one control WebSocket, multiplexed by `id`.
host -> worker:
    {id, kind:"http",     port, method, path, headers, body_b64}
    {id, kind:"ws_open",  port, path,   headers}
    {id, kind:"ws_c2s",   data_b64, binary}
    {id, kind:"ws_close"}
    {id, kind:"cgi",      path, method, query, headers, cookies, body_b64}
worker -> host:
    {id, kind:"resp",      status, headers, body_b64}
    {id, kind:"ws_opened"} | {id, kind:"ws_fail", reason}
    {id, kind:"ws_s2c",    data_b64, binary}
    {id, kind:"ws_closed"}
    {id, kind:"cgi_resp",  status, headers, body_b64}
"""
import argparse, asyncio, base64, contextlib, io, json, logging, subprocess, sys, traceback, types, http.client

try:
    import websockets
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "websockets"], check=True)
    import websockets

log = logging.getLogger("sandbox_proxy")


CGI_WEB_DIR = "/workspace/web"


async def handle_cgi(tunnel, frame):
    """Exec /workspace/web/<path>.py с подменённым stdout. Скрипт top-to-bottom
    исполняется в изолированных globals, печатаемое уходит в body, заголовки
    набираются через header(name, value)."""
    id_ = frame["id"]
    rel = (frame.get("path") or "").lstrip("/")
    fpath = f"{CGI_WEB_DIR}/{rel}"

    request = types.SimpleNamespace(
        method=frame.get("method", "GET"),
        path="/" + rel,
        query=frame.get("query") or {},
        headers=frame.get("headers") or {},
        cookies=frame.get("cookies") or {},
        body=base64.b64decode(frame.get("body_b64") or ""),
    )
    headers: dict = {}
    def header(name, value):
        headers[str(name)] = str(value)

    buf = io.StringIO()
    g = {
        "__name__": "__cgi__", "__file__": fpath,
        "request": request, "header": header,
    }

    try:
        with open(fpath, encoding="utf-8") as f:
            code = compile(f.read(), fpath, "exec")
        with contextlib.redirect_stdout(buf):
            exec(code, g)
        body = buf.getvalue().encode("utf-8")
        status = 200
        headers.setdefault("Content-Type", "text/html; charset=utf-8")
    except FileNotFoundError:
        status, headers, body = 404, {"Content-Type": "text/plain"}, b"Not found"
    except Exception:
        status = 500
        body = (buf.getvalue() + "\n" + traceback.format_exc()).encode("utf-8")
        headers = {"Content-Type": "text/plain; charset=utf-8"}

    await tunnel.send(json.dumps({
        "id": id_, "kind": "cgi_resp",
        "status": status, "headers": headers,
        "body_b64": base64.b64encode(body).decode(),
    }))


async def handle_http(tunnel, frame):
    id_, port = frame["id"], frame["port"]
    body = base64.b64decode(frame.get("body_b64") or "")
    headers = {k: v for k, v in (frame.get("headers") or {}).items() if k.lower() != "host"}

    def _do():
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request(frame["method"], frame["path"], body, headers)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()

    try:
        status, resp_headers, data = await asyncio.to_thread(_do)
    except Exception as e:
        status, resp_headers, data = 502, {"content-type": "text/plain; charset=utf-8"}, f"proxy error: {e}".encode()

    await tunnel.send(json.dumps({
        "id": id_, "kind": "resp",
        "status": status, "headers": resp_headers,
        "body_b64": base64.b64encode(data).decode(),
    }))


class WSSession:
    def __init__(self, tunnel, id_):
        self.tunnel, self.id = tunnel, id_
        self.local = None

    async def open(self, port, path, headers):
        hdrs = {k: v for k, v in (headers or {}).items()
                if k.lower() not in ("host", "sec-websocket-key", "sec-websocket-version",
                                     "sec-websocket-extensions", "connection", "upgrade")}
        try:
            self.local = await websockets.connect(f"ws://127.0.0.1:{port}{path}", extra_headers=hdrs)
        except Exception as e:
            await self.tunnel.send(json.dumps({"id": self.id, "kind": "ws_fail", "reason": str(e)}))
            return
        await self.tunnel.send(json.dumps({"id": self.id, "kind": "ws_opened"}))
        asyncio.create_task(self._pump_s2c())

    async def _pump_s2c(self):
        try:
            async for msg in self.local:
                binary = isinstance(msg, (bytes, bytearray))
                data = bytes(msg) if binary else msg.encode()
                await self.tunnel.send(json.dumps({
                    "id": self.id, "kind": "ws_s2c",
                    "data_b64": base64.b64encode(data).decode(), "binary": binary,
                }))
        except Exception:
            pass
        await self.tunnel.send(json.dumps({"id": self.id, "kind": "ws_closed"}))

    async def c2s(self, frame):
        if not self.local:
            return
        data = base64.b64decode(frame.get("data_b64") or "")
        try:
            await self.local.send(data if frame.get("binary") else data.decode())
        except Exception:
            pass

    async def close(self):
        if self.local:
            try: await self.local.close()
            except Exception: pass


async def run_once(url):
    sessions: dict = {}
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=None) as tunnel:
        log.info("tunnel open")
        try:
            async for raw in tunnel:
                frame = json.loads(raw)
                kind, id_ = frame["kind"], frame["id"]
                if kind == "http":
                    asyncio.create_task(handle_http(tunnel, frame))
                elif kind == "cgi":
                    asyncio.create_task(handle_cgi(tunnel, frame))
                elif kind == "ws_open":
                    sess = WSSession(tunnel, id_)
                    sessions[id_] = sess
                    asyncio.create_task(sess.open(frame["port"], frame["path"], frame.get("headers")))
                elif kind == "ws_c2s":
                    sess = sessions.get(id_)
                    if sess:
                        asyncio.create_task(sess.c2s(frame))
                elif kind == "ws_close":
                    sess = sessions.pop(id_, None)
                    if sess:
                        asyncio.create_task(sess.close())
        finally:
            for s in sessions.values():
                asyncio.create_task(s.close())


async def main(url):
    log.info("connecting to %s", url)
    while True:
        try:
            await run_once(url)
        except Exception as e:
            log.warning("tunnel error: %s", e)
        await asyncio.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    asyncio.run(main(ap.parse_args().url))
