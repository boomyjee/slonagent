"""Stub library for slonagent sandbox scripts.

Usage:
    from agent import Skill, tool, get_agent
    from src.transport.web import WebTransport
"""

import json
import os
import threading


_channel = None
_channel_lock = threading.Lock()


_RPC_ENV_FILE = "/run/slonagent.env"


def get_agent(thread_id: str | None = None):
    """Прокси на host-агента. По умолчанию — текущий (thread_id="").

    Открывает один WebSocket к host'у на первый вызов в процессе, потом
    переиспользует. URL читается из /run/slonagent.env — host пишет туда
    актуальный адрес при ensure_container. Env-переменная SLONAGENT_RPC_URL
    остаётся как fallback (тесты, ручной запуск вне песочницы).
    """
    from rpc import Proxy, _ProxyCall
    result = Proxy(_ensure_channel(), "host").get_agent(thread_id)
    # Host-side make_agent async → Proxy.__call__ возвращает _ProxyCall
    # (обёртка над in-flight future). Разворачиваем синхронно — sandbox-
    # вызов агента из обычного скрипта не должен думать про async.
    return result.wait() if isinstance(result, _ProxyCall) else result


def _ensure_channel():
    global _channel
    with _channel_lock:
        if _channel is None:
            _channel = _open_channel()
        return _channel


def _read_rpc_url() -> str | None:
    try:
        with open(_RPC_ENV_FILE) as f:
            for line in f:
                if line.startswith("SLONAGENT_RPC_URL="):
                    return line[len("SLONAGENT_RPC_URL="):].strip()
    except FileNotFoundError:
        pass
    return os.environ.get("SLONAGENT_RPC_URL")


def _open_channel():
    url = _read_rpc_url()
    if not url:
        raise RuntimeError(
            f"SLONAGENT RPC URL не найден ни в {_RPC_ENV_FILE}, ни в env — "
            "get_agent() работает только из слонагентовской песочницы при "
            "поднятом dashboard-транспорте"
        )
    try:
        from websockets.sync.client import connect
    except ImportError:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "websockets"], check=True)
        from websockets.sync.client import connect

    ws = connect(url)

    def readline():
        try:
            return ws.recv() + "\n"
        except Exception:
            return ""

    def writeline(msg):
        ws.send(json.dumps(msg, ensure_ascii=False))

    from rpc import Channel
    ch = Channel(readline, writeline, ref_prefix="c")
    ch.start()
    return ch


def tool(description: str):
    def decorator(fn):
        fn._is_tool = True
        fn._tool_description = description
        return fn
    return decorator


class Skill:
    def __init__(self):
        self.agent = None  # set by runner to a Proxy

    async def register(self, agent):
        self.agent = agent

    async def start(self):
        pass

    async def get_context_prompt(self, user_text=""):
        return ""

    async def get_tool_prompt(self, tool_name):
        return ""

    async def get_tools(self):
        return []

    def get_bypass_commands(self, standalone_only=False):
        return {}

    async def is_bypass_command(self, text):
        return False

    async def dispatch_bypass(self, text):
        return None

    async def dispatch_tool_call(self, tool_call):
        return None
