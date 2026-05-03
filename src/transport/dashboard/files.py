"""File API: list/read/write/create/rename/delete inside a per-request root.

The server is stateless about "current root" — every endpoint takes the
root host path as a query/body argument. If omitted, the sandbox's
workspace_dir (if a SandboxSkill is mounted) is used as the default.
The client owns root selection (in localStorage) and sends it on each call.

API paths are root-relative and start with `/`: `/foo` resolves to
`<root>/foo`. There are no `..` checks — the dashboard is auth-gated
and meant for the user themselves.
"""
import os
import mimetypes
import shutil
import string
import sys

from fastapi import Query, Request
from fastapi.responses import FileResponse, JSONResponse


class FilesAPI:
    def __init__(self, fork):
        self.fork = fork

    def register(self):
        t = self.fork
        t.register_route("get", "/api/files", self.list)
        t.register_route("get", "/api/file", self.read)
        t.register_route("get", "/api/file/raw", self.read_raw)
        t.register_route("put", "/api/file", self.write)
        t.register_route("post", "/api/file/create", self.create)
        t.register_route("patch", "/api/file/rename", self.rename)
        t.register_route("post", "/api/file/move", self.move)
        t.register_route("post", "/api/file/copy", self.copy)
        t.register_route("delete", "/api/file", self.delete)
        t.register_route("get", "/api/dirs", self.list_dirs)

    def default_root(self) -> str | None:
        return self.fork.default_root

    def resolve(self, root: str | None, path: str) -> str | None:
        actual = root or self.default_root()
        if actual is None or not path.startswith("/"):
            return None
        rel = path.lstrip("/")
        return os.path.join(actual, rel) if rel else actual

    # ─── file ops ──────────────────────────────────────────────────

    async def list(self, root: str = Query(""), path: str = Query("/")):
        host = self.resolve(root, path)
        if host is None:
            return JSONResponse({"error": "No root resolvable"}, 400)
        if not os.path.isdir(host):
            return JSONResponse({"error": f"Not a directory: {path}"}, 400)
        sandbox = self.fork._sandbox
        web_root = os.path.join(sandbox.workspace_dir, "web") if sandbox else None
        entries = []
        for name in sorted(os.listdir(host)):
            full = os.path.join(host, name)
            is_dir = os.path.isdir(full)
            entry = {
                "name": name,
                "is_dir": is_dir,
                "path": path.rstrip("/") + "/" + name,
            }
            if is_dir and os.path.isdir(os.path.join(full, ".git")):
                entry["has_git"] = True
            # Всё что лежит под workspace/web/ (и сам web_dir) доступно через
            # дашборд — даём URL без проверок, сервер сам разберётся
            # (директории резолвятся на index.{html,htm,py}, иначе 404).
            if web_root:
                try:
                    rel = os.path.relpath(full, web_root)
                except ValueError:
                    rel = ".."
                if not rel.startswith(".."):
                    sub = "" if rel == "." else rel.replace(os.sep, "/")
                    entry["url"] = await self.fork.get_url("/web/" + sub)
            entries.append(entry)
        return JSONResponse({"entries": entries})

    async def read(self, root: str = Query(""), path: str = Query(...)):
        host = self.resolve(root, path)
        if host is None:
            return JSONResponse({"error": "No root resolvable"}, 400)
        if not os.path.isfile(host):
            return JSONResponse({"error": f"Not a file: {path}"}, 400)
        try:
            with open(host, encoding="utf-8", errors="replace") as f:
                return JSONResponse({"path": path, "content": f.read()})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    async def read_raw(self, root: str = Query(""), path: str = Query(...)):
        """Stream raw bytes — for images/videos rendered directly by the
        browser. Mime is guessed from extension."""
        host = self.resolve(root, path)
        if host is None:
            return JSONResponse({"error": "No root resolvable"}, 400)
        if not os.path.isfile(host):
            return JSONResponse({"error": f"Not a file: {path}"}, 400)
        mime, _ = mimetypes.guess_type(host)
        return FileResponse(host, media_type=mime or "application/octet-stream")

    async def write(self, request: Request):
        data = await request.json()
        host = self.resolve(data.get("root", ""), data.get("path"))
        if host is None:
            return JSONResponse({"error": "No root resolvable"}, 400)
        try:
            os.makedirs(os.path.dirname(host), exist_ok=True)
            with open(host, "w", encoding="utf-8") as f:
                f.write(data.get("content", ""))
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    async def create(self, request: Request):
        data = await request.json()
        host = self.resolve(data.get("root", ""), data.get("path"))
        if host is None:
            return JSONResponse({"error": "No root resolvable"}, 400)
        if os.path.exists(host):
            return JSONResponse({"error": "Already exists"}, 409)
        kind = data.get("type", "file")
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
        host = self.resolve(data.get("root", ""), path)
        if host is None:
            return JSONResponse({"error": "No root resolvable"}, 400)
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

    async def _bulk_transfer(self, request: Request, op: str):
        """op = 'move' (shutil.move) или 'copy' (shutil.copy2/copytree).
        body: {paths: [src1, src2, ...], dest_dir: '/some/dir', root?}.
        Если у источника и цели одинаковая директория — no-op для этого пути."""
        data = await request.json()
        paths = data.get("paths") or []
        dest_dir = data.get("dest_dir")
        root = data.get("root", "")
        if not isinstance(paths, list) or not dest_dir:
            return JSONResponse({"error": "paths[] и dest_dir обязательны"}, 400)
        dest_host = self.resolve(root, dest_dir)
        if dest_host is None:
            return JSONResponse({"error": "No root resolvable"}, 400)
        if not os.path.isdir(dest_host):
            return JSONResponse({"error": f"Not a directory: {dest_dir}"}, 400)
        moved = []
        errors = {}
        for p in paths:
            src = self.resolve(root, p)
            if src is None:
                errors[p] = "No root resolvable"
                continue
            if not os.path.exists(src):
                errors[p] = "Not found"
                continue
            target = os.path.join(dest_host, os.path.basename(src))
            if os.path.exists(target):
                errors[p] = f"Already exists at {dest_dir}"
                continue
            try:
                if op == "move":
                    shutil.move(src, target)
                else:  # copy
                    if os.path.isdir(src):
                        shutil.copytree(src, target)
                    else:
                        shutil.copy2(src, target)
                moved.append(p)
            except Exception as e:
                errors[p] = str(e)
        return JSONResponse({"status": "ok", "done": moved, "errors": errors})

    async def move(self, request: Request):
        return await self._bulk_transfer(request, "move")

    async def copy(self, request: Request):
        return await self._bulk_transfer(request, "copy")

    async def delete(self, root: str = Query(""), path: str = Query(...)):
        host = self.resolve(root, path)
        if host is None:
            return JSONResponse({"error": "No root resolvable"}, 400)
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

    # ─── host fs browser (for the Change Root picker) ────────────────

    async def list_dirs(self, path: str = Query("")):
        """Lists subdirectories of any host path; with no `path` returns
        drive roots on Windows or `/` on unix. Used by the Change Root
        picker — which traffics in absolute host paths by definition.

        All returned paths are normalized to forward slashes so the
        client's display doesn't mix `E:/dev\\slonagent\\memory`."""
        norm = lambda p: p.replace("\\", "/")
        if not path:
            if sys.platform == "win32":
                entries = []
                for letter in string.ascii_uppercase:
                    drive = f"{letter}:\\"
                    if os.path.exists(drive):
                        entries.append({"name": norm(drive), "path": norm(drive)})
                return JSONResponse({"path": "", "parent": None, "entries": entries})
            return JSONResponse({"path": "", "parent": None,
                                 "entries": [{"name": "/", "path": "/"}]})
        if not os.path.isdir(path):
            return JSONResponse({"error": f"Not a directory: {path}"}, 400)
        entries = []
        try:
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if os.path.isdir(full):
                    entries.append({"name": name, "path": norm(full)})
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, 403)
        parent = os.path.dirname(path.rstrip("/").rstrip(os.sep))
        if parent == path or not parent:
            parent = ""
        return JSONResponse({"path": norm(path), "parent": norm(parent), "entries": entries})
