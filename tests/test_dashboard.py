"""Dashboard API + WS watcher integration tests.

Spins up a DashboardTransport against a temp directory used as the sandbox
workspace, drives it via FastAPI's TestClient (HTTP + WebSocket), and
verifies the file-API behaviour and end-to-end watcher event delivery.
"""
import asyncio
import os
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.transport.web import WebTransport
from src.transport.dashboard import DashboardTransport


class _Sandbox:
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir


class _Agent:
    def __init__(self, workspace_dir: str):
        self.id = "test"
        self.skills = [_Sandbox(workspace_dir)]

    async def process_message(self, *args, **kwargs):
        # BaseTransport binds this on attach; tests don't drive chat.
        pass


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


@pytest.fixture
def transport(workspace, monkeypatch):
    # Pre-build the FastAPI app so _ensure_server short-circuits and never
    # tries to spawn uvicorn / a tunnel.
    WebTransport._app = FastAPI()
    WebTransport._loop = asyncio.new_event_loop()
    WebTransport._sish_domain = ""
    WebTransport._password_hash = ""
    WebTransport._tunnel_url = None
    WebTransport._tunnel_ready = None

    # The real DashboardTransport._sandbox property looks for a SandboxSkill
    # instance in agent.skills; for tests we just inject our stub.
    sandbox_obj = _Sandbox(workspace)
    monkeypatch.setattr(DashboardTransport, "_sandbox",
                        property(lambda self: sandbox_obj))

    t = DashboardTransport()
    t.set_agent(_Agent(workspace))
    yield t
    t.cleanup()
    WebTransport._app = None


@pytest.fixture
def client(transport):
    return TestClient(WebTransport._app)


# ─── HTTP file API ────────────────────────────────────────────────────────────


def test_list_files_default_root(client, workspace):
    open(os.path.join(workspace, "hello.txt"), "w").close()
    r = client.get("/test/dashboard/api/files?path=/")
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["entries"]]
    assert "hello.txt" in names


def test_list_files_explicit_root_overrides_default(client, tmp_path, workspace):
    other = tmp_path.parent / "other_root"
    other.mkdir()
    (other / "marker.py").write_text("# marker")
    r = client.get(f"/test/dashboard/api/files?root={other}&path=/")
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["entries"]]
    assert "marker.py" in names


def test_read_then_write_file(client, workspace):
    r = client.put("/test/dashboard/api/file",
                   json={"path": "/foo.txt", "content": "hi"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.get("/test/dashboard/api/file?path=/foo.txt")
    assert r.status_code == 200
    assert r.json()["content"] == "hi"


def test_create_and_delete(client, workspace):
    r = client.post("/test/dashboard/api/file/create",
                    json={"path": "/sub", "type": "folder"})
    assert r.status_code == 200
    assert os.path.isdir(os.path.join(workspace, "sub"))

    r = client.request("DELETE", "/test/dashboard/api/file?path=/sub")
    assert r.status_code == 200
    assert not os.path.exists(os.path.join(workspace, "sub"))


def test_api_config_dropped(client):
    r = client.get("/test/dashboard/api/config")
    assert r.status_code == 404


def test_api_root_dropped(client):
    r = client.post("/test/dashboard/api/root", json={"root": ""})
    assert r.status_code == 405  # endpoint not registered


def test_dirs_browser(client, tmp_path):
    sub = tmp_path / "child"
    sub.mkdir()
    r = client.get(f"/test/dashboard/api/dirs?path={tmp_path}")
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["entries"]]
    assert "child" in names


# ─── WS-driven watcher ────────────────────────────────────────────────────────


def _drain_until_files_changed(ws, max_messages=20):
    """Skip chat-style replay events; return the first files_changed.
    Blocks per-call (TestClient WS receive_json ignores timeout args)."""
    for _ in range(max_messages):
        msg = ws.receive_json()
        if msg.get("type") == "files_changed":
            return msg
    pytest.fail(f"no files_changed event after {max_messages} messages")


def test_watcher_delivers_event_on_create(client, workspace):
    with client.websocket_connect("/test/dashboard/ws") as ws:
        ws.send_json({"type": "watch", "root": workspace})
        # Give the awatch loop a moment to bind to the dir before we touch it.
        time.sleep(0.4)
        open(os.path.join(workspace, "newfile.txt"), "w").close()
        msg = _drain_until_files_changed(ws)
        assert msg["root"] == workspace
        assert "/newfile.txt" in msg["paths"]
        assert msg["tree"] is True


def test_watcher_subscription_replaces_previous(client, tmp_path):
    a = tmp_path / "rootA"; a.mkdir()
    b = tmp_path / "rootB"; b.mkdir()

    with client.websocket_connect("/test/dashboard/ws") as ws:
        ws.send_json({"type": "watch", "root": str(a)})
        time.sleep(0.3)
        ws.send_json({"type": "watch", "root": str(b)})
        time.sleep(0.4)

        # Touching /a after switch must produce no event for this socket.
        (a / "should-not-arrive.txt").write_text("x")
        (b / "expected.txt").write_text("y")

        msg = _drain_until_files_changed(ws)
        assert msg["root"] == str(b)
        assert "/expected.txt" in msg["paths"]


def test_watcher_default_root_when_empty(client, workspace):
    with client.websocket_connect("/test/dashboard/ws") as ws:
        # Empty root -> server falls back to sandbox.workspace_dir.
        ws.send_json({"type": "watch", "root": ""})
        time.sleep(0.4)
        open(os.path.join(workspace, "from_default.txt"), "w").close()
        msg = _drain_until_files_changed(ws)
        assert msg["root"] == workspace
        assert "/from_default.txt" in msg["paths"]


def test_watcher_cleanup_on_disconnect(transport, client, workspace):
    pool = transport._watchers
    with client.websocket_connect("/test/dashboard/ws") as ws:
        ws.send_json({"type": "watch", "root": workspace})
        time.sleep(0.3)
        assert workspace in pool._watchers
    # Allow the on_ws_close hook to run.
    time.sleep(0.3)
    assert workspace not in pool._watchers
