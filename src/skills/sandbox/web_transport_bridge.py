"""Host-side bridge for sandbox WebTransport.

Creates a real WebTransport on the host that proxies route handlers
and ws_handle_message back to the sandbox via RPC Channel.
"""

import logging
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from src.transport.web import WebTransport

log = logging.getLogger(__name__)


class WebTransportBridge(WebTransport):
    """Host-side WebTransport that proxies HTTP/WS requests to sandbox."""

    def __init__(self, prefix, verbose, callback, routes, has_ws_handler, ui_dir=None):
        super().__init__(prefix=prefix, verbose=verbose)
        self._callback = callback
        self._sandbox_routes = routes
        self._has_ws_handler = has_ws_handler
        self._sandbox_ui_dir = Path(ui_dir) if ui_dir else None

    @property
    def _ui_dirs(self) -> list[Path]:
        dirs = []
        if self._sandbox_ui_dir and self._sandbox_ui_dir.is_dir():
            dirs.append(self._sandbox_ui_dir)
        dirs.append(self._BASE_UI)
        return dirs

    def register_routes(self):
        for method, path, handler_name in self._sandbox_routes:
            async def proxy_handler(req: Request, _name=handler_name):
                body = None
                if req.method in ("POST", "PUT", "PATCH"):
                    try:
                        body = await req.json()
                    except Exception:
                        pass
                result = await self._callback.handle_route(
                    _name, query=dict(req.query_params), body=body)
                return JSONResponse(result)
            proxy_handler.__name__ = f"sandbox_{handler_name}"
            self.register_route(method, path, proxy_handler)
        super().register_routes()

    async def ws_handle_message(self, msg):
        if self._has_ws_handler:
            await self._callback.ws_handle_message(msg)
        else:
            await super().ws_handle_message(msg)


class WebTransportFactory:
    def __init__(self, sandbox_skill):
        self._sandbox_skill = sandbox_skill

    def create(self, agent, prefix="", verbose=True, routes=None,
               has_ws_handler=False, callback=None, ui_dir=None):
        # Called from an RPC worker thread. Pure-sync: WebTransport._ensure_server
        # schedules its server/tunnel tasks onto WebTransport._loop internally,
        # so this function has no thread affinity.
        host_ui_dir = self._sandbox_skill.resolve_path(ui_dir) if ui_dir else None
        t = WebTransportBridge(prefix, verbose, callback, routes or [], has_ws_handler, host_ui_dir)
        t.set_agent(agent)
        # Route incoming WS messages through the sandbox WebTransport stub
        # so the sub-agent's MultiTransport sees them and can fan out to
        # sibling transports (e.g. mirror the user's message to telegram
        # via inject_message). Without this, set_agent wires on_message
        # directly to agent.process_message and bypasses MultiTransport.
        t.on_message = callback.process_message
        return t
