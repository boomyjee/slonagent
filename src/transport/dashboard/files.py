"""File API: list/read/write/create/rename/delete inside the sandbox workspace.

Owns its own FastAPI route handlers so the dashboard module stays a thin shell.
All paths are workspace-scoped; sandbox.resolve_path() enforces the boundary.
"""
import os
import mimetypes
import shutil

from fastapi import Query, Request
from fastapi.responses import FileResponse, JSONResponse


class FilesAPI:
    def __init__(self, transport):
        self.transport = transport

    @property
    def _sandbox(self):
        return self.transport._sandbox

    def register(self):
        t = self.transport
        t.register_route("get", "/api/files", self.list)
        t.register_route("get", "/api/file", self.read)
        t.register_route("get", "/api/file/raw", self.read_raw)
        t.register_route("put", "/api/file", self.write)
        t.register_route("post", "/api/file/create", self.create)
        t.register_route("patch", "/api/file/rename", self.rename)
        t.register_route("delete", "/api/file", self.delete)

    def _resolve(self, path: str) -> str | None:
        sandbox = self._sandbox
        if not sandbox:
            return None
        return sandbox.resolve_path(path)

    async def list(self, path: str = Query("/")):
        host = self._resolve(path)
        if host is None:
            return JSONResponse({"error": "No sandbox or access denied"}, 503)
        if not os.path.isdir(host):
            return JSONResponse({"error": f"Not a directory: {path}"}, 400)
        entries = []
        for name in sorted(os.listdir(host)):
            if name.startswith("."):
                continue
            full = os.path.join(host, name)
            is_dir = os.path.isdir(full)
            entry = {
                "name": name,
                "is_dir": is_dir,
                "path": path.rstrip("/") + "/" + name,
            }
            if is_dir and os.path.isdir(os.path.join(full, ".git")):
                entry["has_git"] = True
            entries.append(entry)
        return JSONResponse({"entries": entries})

    async def read(self, path: str = Query(...)):
        host = self._resolve(path)
        if host is None:
            return JSONResponse({"error": "No sandbox or access denied"}, 503)
        if not os.path.isfile(host):
            return JSONResponse({"error": f"Not a file: {path}"}, 400)
        try:
            with open(host, encoding="utf-8", errors="replace") as f:
                return JSONResponse({"path": path, "content": f.read()})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    async def read_raw(self, path: str = Query(...)):
        """Stream raw bytes — for images/videos rendered directly by the
        browser. Mime is guessed from extension."""
        host = self._resolve(path)
        if host is None:
            return JSONResponse({"error": "No sandbox or access denied"}, 503)
        if not os.path.isfile(host):
            return JSONResponse({"error": f"Not a file: {path}"}, 400)
        mime, _ = mimetypes.guess_type(host)
        return FileResponse(host, media_type=mime or "application/octet-stream")

    async def write(self, request: Request):
        data = await request.json()
        path, content = data.get("path"), data.get("content", "")
        host = self._resolve(path)
        if host is None:
            return JSONResponse({"error": "No sandbox or access denied"}, 503)
        try:
            os.makedirs(os.path.dirname(host), exist_ok=True)
            with open(host, "w", encoding="utf-8") as f:
                f.write(content)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    async def create(self, request: Request):
        data = await request.json()
        path, kind = data.get("path"), data.get("type", "file")
        host = self._resolve(path)
        if host is None:
            return JSONResponse({"error": "No sandbox or access denied"}, 503)
        if os.path.exists(host):
            return JSONResponse({"error": f"Already exists: {path}"}, 409)
        try:
            if kind == "folder":
                os.makedirs(host)
            else:
                os.makedirs(os.path.dirname(host), exist_ok=True)
                open(host, "w", encoding="utf-8").close()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    async def rename(self, request: Request):
        data = await request.json()
        path, name = data.get("path"), data.get("name", "")
        if not name or "/" in name or "\\" in name:
            return JSONResponse({"error": "Invalid name"}, 400)
        host = self._resolve(path)
        if host is None:
            return JSONResponse({"error": "No sandbox or access denied"}, 503)
        if not os.path.exists(host):
            return JSONResponse({"error": f"Not found: {path}"}, 404)
        new_host = os.path.join(os.path.dirname(host), name)
        if os.path.exists(new_host):
            return JSONResponse({"error": f"Already exists: {name}"}, 409)
        try:
            os.rename(host, new_host)
            new_path = path.rsplit("/", 1)[0] + "/" + name
            return JSONResponse({"status": "ok", "path": new_path})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    async def delete(self, path: str = Query(...)):
        host = self._resolve(path)
        if host is None:
            return JSONResponse({"error": "No sandbox or access denied"}, 503)
        if not os.path.exists(host):
            return JSONResponse({"error": f"Not found: {path}"}, 404)
        try:
            if os.path.isdir(host):
                shutil.rmtree(host)
            else:
                os.remove(host)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)
