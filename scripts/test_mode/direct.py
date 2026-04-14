"""Direct test harness for sandbox_codingmode_launch without telegram.

Creates an Agent with a verbose DummyTransport, dispatches the tool call,
watches for the coding-mode URL, connects a WebSocket client, sends a
user message, and prints streaming events so we can verify that chunks
are fanning out through the sandbox RPC bridge.
"""
import asyncio, json, logging, os, re, sys
sys.path.insert(0, os.getcwd())
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

with open("scripts/test_mode/.config.json", encoding="utf-8") as f:
    config = json.load(f)
os.environ.update(config.get("env", {}))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

from agent import Agent
from src.transport.base import BaseTransport
from src.transport.dashboard import DashboardTransport


url_event = asyncio.Event()
coding_url = ""
first_response_done = asyncio.Event()
injection_seen = asyncio.Event()
files_changed_seen = asyncio.Event()
last_files_changed_payload = None


class DummyTransport(BaseTransport):
    async def send_message(self, text, stream_id=None, final=True):
        tag = f"stream={stream_id} final={final}" if stream_id is not None else "oneshot"
        print(f"[msg {tag}] {text}", flush=True)
        global coding_url
        m = re.search(r"(https?://[^\s]+/coding/)", text)
        if m and not coding_url:
            coding_url = m.group(1)
            url_event.set()
    async def send_thinking(self, text="", stream_id=None, final=False):
        tag = f"stream={stream_id} final={final}"
        print(f"[think {tag}] {text[:80]}", flush=True)
    async def send_processing(self, active):
        print(f"[processing] active={active}", flush=True)
    async def send_system_prompt(self, *a, **k): pass
    async def on_tool_call(self, name, args):
        print(f"[tool_call] {name} {args}", flush=True)
    async def on_tool_result(self, name, result):
        print(f"[tool_result] {name} {result}", flush=True)
    async def inject_message(self, text, *a, **k):
        print(f"[inject_message] {text}", flush=True)
    async def send_app_url(self, url, text, button=""):
        print(f"[app_url] {url} {text}", flush=True)


async def ws_driver(user_text: str):
    """Wait for coding-mode URL, connect WS, send process_message, idle."""
    import websockets
    await url_event.wait()
    # Bypass tunnel + auth: rewrite host to localhost so auth_middleware lets us in.
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(coding_url)
    port = config["web"].get("port", 8765)
    local = urlunsplit(("ws", f"localhost:{port}", parts.path, "", ""))
    ws_url = local + "ws"
    print(f"[ws] connecting to {ws_url}", flush=True)
    async with websockets.connect(ws_url) as ws:
        print(f"[ws] connected, sending: {user_text}", flush=True)
        await ws.send(json.dumps({
            "type": "transport",
            "method": "process_message",
            "content_parts": [{"type": "text", "text": user_text}],
        }))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                ev = json.loads(raw)
                m = ev.get("method", "?")
                if m == "send_message":
                    print(f"[ws<- msg stream={ev.get('stream_id')} final={ev.get('final')}] {ev.get('text','')[:120]}", flush=True)
                    if ev.get("final"):
                        first_response_done.set()
                elif m == "send_thinking":
                    print(f"[ws<- think stream={ev.get('stream_id')} final={ev.get('final')}] {ev.get('text','')[:80]}", flush=True)
                elif m in ("on_tool_call", "on_tool_result"):
                    print(f"[ws<- {m}] {ev.get('name')}", flush=True)
                elif m == "inject_message":
                    print(f"[ws<- INJECT] {ev.get('text','')[:120]}", flush=True)
                    injection_seen.set()
                elif ev.get("type") == "files_changed":
                    global last_files_changed_payload
                    last_files_changed_payload = ev
                    print(f"[ws<- FILES_CHANGED] paths={ev.get('paths')} tree={ev.get('tree')}", flush=True)
                    files_changed_seen.set()
                else:
                    print(f"[ws<- {m}] {ev}", flush=True)
        except asyncio.TimeoutError:
            print("[ws] idle timeout, closing", flush=True)
        except Exception as e:
            print(f"[ws] recv ended: {e}", flush=True)


async def direction1_driver(transport):
    """Simulate a message coming in via the host's main transport (telegram
    equivalent) by invoking transport.on_message and watch whether it's
    mirrored to the sandbox WebSocket via inject_message."""
    await first_response_done.wait()
    await asyncio.sleep(0.5)
    text = "Сообщение из «телеграма» (direction 1)"
    print(f"[dir1] firing on_message via host transport: {text}", flush=True)
    await transport.on_message([{"type": "text", "text": text}])
    try:
        await asyncio.wait_for(injection_seen.wait(), timeout=5)
        print("[dir1] ✅ injection reached WS client", flush=True)
    except asyncio.TimeoutError:
        print("[dir1] ❌ no inject_message seen within 5s", flush=True)


async def file_change_driver():
    """Modify a file in the workspace and watch for files_changed event."""
    await first_response_done.wait()
    await asyncio.sleep(1.5)  # give watcher time to start + initial poll
    workspace = os.path.join(os.path.dirname(os.path.abspath("scripts/test_mode/__main__.py")),
                             "memory", "workspace")
    test_file = os.path.join(workspace, "hello.py")
    print(f"[fs] writing {test_file}", flush=True)
    with open(test_file, "w", encoding="utf-8") as f:
        f.write(f"# touched at {asyncio.get_event_loop().time()}\n")
    try:
        await asyncio.wait_for(files_changed_seen.wait(), timeout=10)
        print("[fs] ✅ files_changed reached WS client", flush=True)
    except asyncio.TimeoutError:
        print("[fs] ❌ no files_changed event within 10s", flush=True)


async def http_api_driver():
    """Hit /api/config via HTTP to exercise register_json_route end-to-end."""
    import aiohttp
    await url_event.wait()
    await asyncio.sleep(1)
    port = config["web"].get("port", 8765)
    from urllib.parse import urlsplit
    parts = urlsplit(coding_url)
    api_url = f"http://localhost:{port}{parts.path}api/config"
    print(f"[http] GET {api_url}", flush=True)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = await r.text()
                print(f"[http] status={r.status} body={body[:200]}", flush=True)
                if r.status == 200 and "root_path" in body:
                    print("[http] ✅ /api/config reached sandbox handler", flush=True)
                else:
                    print("[http] ❌ unexpected response", flush=True)
    except Exception as e:
        print(f"[http] ❌ error: {e}", flush=True)


async def main():
    tool_name = sys.argv[1] if len(sys.argv) > 1 else "sandbox_codingmode_launch"
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    user_text = sys.argv[3] if len(sys.argv) > 3 else "Привет! Напиши в чате «pong» три раза подряд."
    DashboardTransport.set_server_config(**config.get("web", {}))
    transport = DummyTransport()
    agent = Agent.from_config(
        config["agent"], id="main", transport=transport,
        agent_dir=os.path.dirname(os.path.abspath("scripts/test_mode/__main__.py")),
    )
    await agent.start(run_loop=False)
    print(f"[test] dispatching {tool_name}", flush=True)
    tool_task = asyncio.create_task(agent.dispatch_tool_calls({"tool_calls": [{
        "id": "cli", "function": {"name": tool_name, "arguments": json.dumps(args)},
    }]}))
    ws_task = asyncio.create_task(ws_driver(user_text))
    dir1_task = asyncio.create_task(direction1_driver(transport))
    http_task = asyncio.create_task(http_api_driver())
    fs_task = asyncio.create_task(file_change_driver())
    try:
        await asyncio.wait_for(asyncio.gather(tool_task, ws_task, dir1_task, http_task, fs_task), timeout=120)
    except asyncio.TimeoutError:
        print("[test] total timeout 120s", flush=True)
    finally:
        for t in (tool_task, ws_task, dir1_task, http_task, fs_task):
            if not t.done():
                t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
