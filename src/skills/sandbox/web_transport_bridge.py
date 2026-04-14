"""Host-side bridge for sandbox WebTransport.

Factory function returns a dynamically-built WebTransport subclass that
captures the SandboxSkill in its closure (needed to resolve the
sandbox-side ui_dir into a host path). Registered by name into the RPC
channel, so sandbox constructs it directly via
`Proxy(ch, "WebTransportBridge")(...)` — no intermediate factory object.
"""

import logging
from pathlib import Path

from src.transport.web import WebTransport

log = logging.getLogger(__name__)


def WebTransportBridge(sandbox_skill):
    class _Impl(WebTransport):
        """Host-side WebTransport that proxies HTTP/WS requests to sandbox."""

        def __init__(self, prefix="", verbose=True, ui_dir=None, proxy=None):
            super().__init__(prefix=prefix, verbose=verbose)
            self._proxy = proxy

            sandbox_ui_dir = sandbox_skill.resolve_path(ui_dir) if ui_dir else None
            self._sandbox_ui_dir = Path(sandbox_ui_dir) if sandbox_ui_dir else None

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

        async def ws_handle_message(self, msg):
            await super().ws_handle_message(msg)
            await self._proxy.ws_handle_message(msg)

    return _Impl
