"""Host-side bridge for sandbox WebTransport.

Factory function returns a dynamically-built WebTransport subclass that
captures the SandboxSkill in its closure (needed to resolve the
sandbox-side ui_dir into a host path). Registered by name into the RPC
channel, so sandbox constructs it directly via
`Proxy(ch, "WebTransportBridge")(...)` — no intermediate factory object.

Sandbox-side stub holds a single proxy and calls all WebTransport
methods on it (transport-level + fork-level mixed). Host-side bridge
splits into _BridgeTransport (BaseTransport adapter) and _BridgeFork
(per-agent shared state); transport shims fork-level methods so the
sandbox protocol stays intact.
"""

import logging
from pathlib import Path

from src.transport.web import WebTransport, WebFork

log = logging.getLogger(__name__)


def WebTransportBridge(sandbox_skill):

    class _BridgeFork(WebFork):
        def __init__(self, ref_agent, prefix, ui_dir, proxy):
            self.prefix = prefix
            self._sandbox_ui_dir = ui_dir
            self._proxy = proxy
            super().__init__(ref_agent)

        @property
        def _ui_dirs(self) -> list[Path]:
            dirs = []
            if self._sandbox_ui_dir and self._sandbox_ui_dir.is_dir():
                dirs.append(self._sandbox_ui_dir)
            dirs.append(self._BASE_UI)
            return dirs

        def register_routes(self):
            return self._proxy.register_routes()

        def super_register_routes(self):
            return super().register_routes()

        async def ws_handle_message(self, msg, ws=None):
            await super().ws_handle_message(msg, ws)
            await self._proxy.ws_handle_message(msg)

    class _BridgeTransport(WebTransport):
        def __init__(self, prefix="", verbose=True, ui_dir=None, proxy=None):
            sandbox_ui = sandbox_skill.resolve_path(ui_dir) if ui_dir else None
            self._bridge_prefix = prefix
            self._bridge_ui_dir = Path(sandbox_ui) if sandbox_ui else None
            self._bridge_proxy = proxy
            super().__init__(verbose=verbose)

        def create_fork(self, agent):
            return _BridgeFork(
                ref_agent=agent,
                prefix=self._bridge_prefix,
                ui_dir=self._bridge_ui_dir,
                proxy=self._bridge_proxy,
            )

        # Sandbox-стаб обращается к bridge как к старому монолитному
        # WebTransport — делегируем fork-level методы в self.fork, пока
        # на той стороне не разделим протокол явно.
        def register_route(self, method, path, handler):
            self.fork.register_route(method, path, handler)

        def register_json_route(self, method, path, handler):
            self.fork.register_json_route(method, path, handler)

        def register_routes(self):
            return self.fork.register_routes()

        def super_register_routes(self):
            return self.fork.super_register_routes()

        async def ws_handle_message(self, msg):
            await self.fork.ws_handle_message(msg)

        async def get_url(self, sub_path=""):
            return await self.fork.get_url(sub_path)

        async def get_auth_url(self, sub_path=""):
            return await self.fork.get_auth_url(sub_path)

        async def send(self, event, replay=False):
            await self.fork.send(event, replay=replay)

    return _BridgeTransport
