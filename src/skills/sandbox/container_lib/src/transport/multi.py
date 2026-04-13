"""Sandbox shim for MultiTransport.

Preserves the host import path `from src.transport.multi import MultiTransport`
so sandbox scripts read identically to host code. The real `MultiTransport`
lives on the host — this shim just constructs one there via the sandbox's
single active RPC channel and returns the resulting Proxy.
"""
from rpc import Proxy, active_channel


def MultiTransport(transports):
    ch = active_channel()
    if ch is None:
        raise RuntimeError("No active RPC channel — MultiTransport shim needs rpc.Channel to be started")
    return Proxy(ch, "MultiTransport")(transports)
