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


url_event = asyncio.Event()
coding_url = ""


class DummyTransport(BaseTransport):
    async def send_message(self, text, stream_id=None, final=True):
        tag = f"stream={stream_id} final={final}" if stream_id is not None else "oneshot"
        print(f"[msg {tag}] {text}", flush=True)
        global coding_url
        m = re.search(r"(http://[^\s]+/coding/)", text)
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
        print(f"[tool_result] {name} {str(result)[:200]}", flush=True)
    async def inject_message(self, *a, **k): pass
    async def send_app_url(self, url, text, button=""):
        print(f"[app_url] {url} {text}", flush=True)


async def ws_driver(user_text: str):
    """Wait for coding-mode URL, connect WS, send process_message, idle."""
    import websockets
    await url_event.wait()
    ws_url = coding_url.replace("http://", "ws://") + "ws"
    print(f"[ws] connecting to {ws_url}", flush=True)
    async with websockets.connect(ws_url) as ws:
        print(f"[ws] connected, sending: {user_text}", flush=True)
        await ws.send(json.dumps({
            "type": "transport",
            "method": "process_message",
            "content_parts": [{"type": "text", "text": user_text}],
        }))
        # Consume events from the server (optional visibility).
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                ev = json.loads(raw)
                m = ev.get("method", "?")
                if m == "send_message":
                    print(f"[ws<- msg stream={ev.get('stream_id')} final={ev.get('final')}] {ev.get('text','')[:120]}", flush=True)
                elif m == "send_thinking":
                    print(f"[ws<- think stream={ev.get('stream_id')} final={ev.get('final')}] {ev.get('text','')[:80]}", flush=True)
                elif m in ("on_tool_call", "on_tool_result"):
                    print(f"[ws<- {m}] {ev.get('name')}", flush=True)
        except asyncio.TimeoutError:
            print("[ws] idle timeout, closing", flush=True)
        except Exception as e:
            print(f"[ws] recv ended: {e}", flush=True)


async def main():
    tool_name = sys.argv[1] if len(sys.argv) > 1 else "sandbox_codingmode_launch"
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    user_text = sys.argv[3] if len(sys.argv) > 3 else "Привет! Напиши в чате «pong» три раза подряд."
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
    try:
        await asyncio.wait_for(asyncio.gather(tool_task, ws_task), timeout=120)
    except asyncio.TimeoutError:
        print("[test] total timeout 120s", flush=True)
    finally:
        for t in (tool_task, ws_task):
            if not t.done():
                t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
