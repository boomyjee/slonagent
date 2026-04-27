"""Async wrapper over the `git` CLI plus FastAPI endpoint handlers.

`Git` shells out via asyncio.create_subprocess_exec in the repo directory.
`GitAPI` exposes them as JSON HTTP endpoints, scoped to a workspace path
(resolved through the sandbox).
"""
import asyncio
import os

from fastapi import Query, Request
from fastapi.responses import JSONResponse


class Git:
    def __init__(self, repo_dir: str):
        self.dir = repo_dir

    async def run(self, *args: str) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=self.dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode

    async def current_branch(self) -> str:
        out, _, _ = await self.run("rev-parse", "--abbrev-ref", "HEAD")
        return out.strip()

    async def status(self) -> list[dict]:
        # Porcelain v1: each line is "XY path" (or "XY old -> new" for renames).
        # X = index status, Y = working-tree status. '?' means untracked.
        out, _, _ = await self.run("status", "--porcelain", "--untracked-files")
        files = []
        for line in out.splitlines():
            if len(line) < 3:
                continue
            x, y, path = line[0], line[1], line[3:]
            old_path = None
            if x == "R":
                parts = path.split(" -> ", 1)
                if len(parts) == 2:
                    old_path, path = parts
            staged = x not in (" ", "?")
            partial = staged and y not in (" ", "")
            files.append({
                "file": path,
                "old_file": old_path,
                "state": x + y,
                "staged": staged,
                "partial": partial,
            })
        return files

    async def diff(self, file: str, staged: bool = False) -> str:
        args = ["diff", "--no-color"]
        if staged:
            args.append("--cached")
        else:
            args.append("HEAD")
        args += ["--", file]
        out, _, _ = await self.run(*args)
        return out

    async def stage(self, file: str) -> tuple[str, int]:
        out, err, code = await self.run("add", "--", file)
        return (out + err), code

    async def unstage(self, file: str) -> tuple[str, int]:
        out, err, code = await self.run("reset", "HEAD", "--", file)
        return (out + err), code

    async def commit(self, message: str) -> tuple[str, int]:
        out, err, code = await self.run("commit", "-m", message)
        return (out + err), code


class GitAPI:
    def __init__(self, transport):
        self.transport = transport

    @property
    def _sandbox(self):
        return self.transport._sandbox

    def register(self):
        t = self.transport
        t.register_route("get", "/api/git/status", self.status)
        t.register_route("get", "/api/git/diff", self.diff)
        t.register_route("post", "/api/git/stage", self.stage)
        t.register_route("post", "/api/git/commit", self.commit)

    def _git(self, path: str):
        sandbox = self._sandbox
        if not sandbox:
            return None, JSONResponse({"error": "No sandbox"}, 503)
        host = sandbox.resolve_path(path)
        if host is None:
            return None, JSONResponse({"error": f"Access denied: {path}"}, 403)
        if not os.path.isdir(host):
            return None, JSONResponse({"error": f"Not a directory: {path}"}, 400)
        return Git(host), None

    async def status(self, path: str = Query(...)):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"branch": await git.current_branch(), "files": await git.status()})

    async def diff(self, path: str = Query(...), file: str = Query(...), staged: bool = Query(False)):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"diff": await git.diff(file, staged=staged)})

    async def stage(self, request: Request):
        data = await request.json()
        git, err = self._git(data["path"])
        if err: return err
        out, code = await (git.stage(data["file"]) if data.get("staged") else git.unstage(data["file"]))
        return JSONResponse({"status": "ok" if code == 0 else "error", "output": out})

    async def commit(self, request: Request):
        data = await request.json()
        git, err = self._git(data["path"])
        if err: return err
        message = (data.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "Empty commit message"}, 400)
        out, code = await git.commit(message)
        return JSONResponse({"status": "ok" if code == 0 else "error", "output": out})
