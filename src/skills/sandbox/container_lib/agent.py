"""Stub library for slonagent sandbox scripts.

Usage:
    from agent import Skill, tool, get_agent
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
    """Один канал на процесс с автореконнектом. Проверяем сам ws (state на
    websockets.sync.client) — если не OPEN, закрываем старый Channel и
    открываем новый. Reader-thread мёртвого канала уже отработал (read EOF
    → _fail_pending), pending wait() получили error → caller их обработал."""
    global _channel
    with _channel_lock:
        if _channel is not None and not _ws_open(_channel._ws):
            try: _channel.close()
            except Exception: pass
            try: _channel._ws.close()
            except Exception: pass
            _channel = None
        if _channel is None:
            _channel = _open_channel()
        return _channel


def _ws_open(ws) -> bool:
    try:
        from websockets.protocol import State
        return ws.state == State.OPEN
    except Exception:
        return False


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
        # Пустая recv (нормальное закрытие) → "" → reader-thread выходит из
        # цикла → _fail_pending сбрасывает pending wait()'ы с error. Старый
        # код возвращал "\n" на recv()=="" и reader не выходил.
        try:
            data = ws.recv()
        except Exception:
            return ""
        return (data + "\n") if data else ""

    def writeline(msg):
        try:
            ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            # Принудительно закрываем — reader увидит EOF, сбросит pending,
            # caller на .wait() получит exception (не deadlock).
            try: ws.close()
            except Exception: pass
            raise

    from rpc import Channel
    ch = Channel(readline, writeline, ref_prefix="c")
    ch._ws = ws  # для _ensure_channel: проверять живой ли ws
    ch.start()
    return ch


def tool(description: str):
    def decorator(fn):
        fn._is_tool = True
        fn._tool_description = description
        return fn
    return decorator


class Skill:
    _thread_id: str = ""  # выставляется runner'ом перед вызовом тула

    @property
    def agent(self):
        """Прокси на host-агента для текущего thread_id. Лениво открывает
        WS на /agent-rpc при первом обращении, кеширует на инстансе —
        тулы не трогающие агента не платят за RPC-канал."""
        if not hasattr(self, "_agent_cache"):
            self._agent_cache = get_agent(self._thread_id)
        return self._agent_cache

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
