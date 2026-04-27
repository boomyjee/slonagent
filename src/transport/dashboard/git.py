"""Async wrapper over the `git` CLI plus FastAPI endpoint handlers.

`Git` shells out via asyncio.create_subprocess_exec in the repo directory.
`GitAPI` exposes them as JSON HTTP endpoints, scoped to a workspace path
(resolved through the sandbox).
"""
import asyncio
import hashlib
import json
import os
import re

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

    async def branches(self) -> list[str]:
        out, _, _ = await self.run("branch", "--format=%(refname:short)")
        return [b.strip() for b in out.splitlines() if b.strip()]

    async def switch_branch(self, name: str) -> tuple[str, int]:
        out, err, code = await self.run("checkout", "--quiet", name)
        return (out + err), code

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

    async def diff_between(self, file: str, sha1: str, sha2: str) -> str:
        out, _, _ = await self.run("diff", "--no-color", sha1, sha2, "--", file)
        return out

    async def stage(self, file: str) -> tuple[str, int]:
        if os.path.exists(os.path.join(self.dir, file)):
            out, err, code = await self.run("add", "--", file)
        else:
            out, err, code = await self.run("rm", "--", file)
        return (out + err), code

    async def unstage(self, file: str) -> tuple[str, int]:
        out, err, code = await self.run("reset", "HEAD", "--", file)
        return (out + err), code

    async def checkout_file(self, file: str, old_file: str | None = None) -> tuple[str, int]:
        # Match dayside: reset HEAD, checkout -f, clean -f. For renames, also
        # reset/checkout the old name so a rename-in-progress fully reverts.
        combined = ""
        for f in filter(None, [old_file, file]):
            for cmd in (["reset", "HEAD", "--", f], ["checkout", "-f", "--", f], ["clean", "-f", "--", f]):
                out, err, _ = await self.run(*cmd)
                combined += out + err
        return combined, 0

    async def commit(self, message: str) -> tuple[str, int]:
        out, err, code = await self.run("commit", "-m", message)
        return (out + err), code

    async def amend(self, message: str | None) -> tuple[str, int]:
        args = ["commit", "--amend"]
        if message:
            args += ["-m", message]
        else:
            args.append("--no-edit")
        out, err, code = await self.run(*args)
        return (out + err), code

    async def show_file(self, file: str, ref: str) -> str:
        out, _, _ = await self.run("show", f"{ref}:{file}")
        return out

    async def rev_parse(self, ref: str) -> str:
        out, _, _ = await self.run("rev-parse", "--short", ref)
        return out.strip()

    async def last_commits(self, branch: str, count: int, skip: int = 0) -> list[dict]:
        args = ["log", f"--max-count={count}"]
        if skip:
            args.append(f"--skip={skip}")
        args += ["--pretty=format:%h>%H>%cd>%s", "--date=iso8601", branch]
        out, _, _ = await self.run(*args)
        commits = []
        for line in out.splitlines():
            parts = line.split(">", 3)
            if len(parts) == 4:
                commits.append({
                    "sha_short": parts[0],
                    "sha_full": parts[1],
                    "date": parts[2],
                    "message": parts[3],
                })
        return commits

    async def commits_between(self, sha1: str, sha2: str) -> list[dict]:
        out, _, _ = await self.run(
            "log", "--pretty=format:%h>%H>%cd>%s", "--date=iso8601",
            "--ancestry-path", f"{sha1}^..{sha2}",
        )
        commits = []
        for line in out.splitlines():
            parts = line.split(">", 3)
            if len(parts) == 4:
                commits.append({
                    "sha_short": parts[0],
                    "sha_full": parts[1],
                    "date": parts[2],
                    "message": parts[3],
                })
        return commits

    async def history(self, commit_sha: str, depth: int = 1) -> list[dict]:
        out, err, _ = await self.run(
            "diff", "--no-color", "--name-status", "--find-renames",
            f"{commit_sha}~{depth}", commit_sha,
        )
        if err and not out:
            return []
        files = []
        for line in out.splitlines():
            parts = re.split(r"\s+", line.strip(), maxsplit=2)
            if len(parts) < 2:
                continue
            state = parts[0]
            if state.startswith("R") and len(parts) == 3:
                files.append({"file": parts[2], "old_file": parts[1], "state": state})
            else:
                files.append({"file": parts[1], "old_file": None, "state": state})
        return files

    _BLAME_RE = re.compile(
        r"^([^\s]+)\s+(?:[^\s]+\s+?)?(\d+)\s+\((.+?)\s+(\d{4}-\d{2}-\d{2}) "
        r"(\d{2}:\d{2}:\d{2}) [\+\-]\d{4}\s+(\d+)\) (.*)$"
    )

    async def blame(self, file: str, ref: str | None = None) -> list[dict]:
        args = ["blame", "-l", "-n", "--date=iso8601"]
        if ref:
            args.append(ref)
        args += ["--", file]
        out, _, _ = await self.run(*args)
        lines = []
        for ln in out.splitlines():
            m = self._BLAME_RE.match(ln)
            if not m:
                continue
            lines.append({
                "sha": m.group(1),
                "author": m.group(3).strip(),
                "date": m.group(4),
                "content": m.group(7),
            })
        return lines

    @staticmethod
    def status_hash(files: list[dict]) -> str:
        return hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()


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
        t.register_route("get", "/api/git/diff_between", self.diff_between)
        t.register_route("get", "/api/git/show", self.show)
        t.register_route("get", "/api/git/blame", self.blame)
        t.register_route("get", "/api/git/blame_at", self.blame_at)
        t.register_route("get", "/api/git/branches", self.branches)
        t.register_route("get", "/api/git/log", self.log)
        t.register_route("get", "/api/git/history", self.history)
        t.register_route("post", "/api/git/stage", self.stage)
        t.register_route("post", "/api/git/checkout", self.checkout)
        t.register_route("post", "/api/git/commit", self.commit)
        t.register_route("post", "/api/git/amend", self.amend)
        t.register_route("post", "/api/git/switch_branch", self.switch_branch)

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
        files = await git.status()
        return JSONResponse({
            "branch": await git.current_branch(),
            "files": files,
            "status_hash": Git.status_hash(files),
        })

    async def diff(self, path: str = Query(...), file: str = Query(...), staged: bool = Query(False)):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"diff": await git.diff(file, staged=staged)})

    async def diff_between(self, path: str = Query(...), file: str = Query(...),
                           sha1: str = Query(...), sha2: str = Query(...)):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"diff": await git.diff_between(file, sha1, sha2)})

    async def show(self, path: str = Query(...), file: str = Query(...), ref: str = Query("HEAD")):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"content": await git.show_file(file, ref)})

    async def blame(self, path: str = Query(...), file: str = Query(...), ref: str = Query("")):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"lines": await git.blame(file, ref or None)})

    async def blame_at(self, path: str = Query(...)):
        """Blame for a file at workspace path. Auto-discovers the repo via
        `git rev-parse --show-toplevel`, so the caller doesn't need to know
        which folder owns the .git."""
        sandbox = self._sandbox
        if not sandbox:
            return JSONResponse({"error": "No sandbox"}, 503)
        host = sandbox.resolve_path(path)
        if host is None:
            return JSONResponse({"error": f"Access denied: {path}"}, 403)
        if not os.path.isfile(host):
            return JSONResponse({"error": f"Not a file: {path}"}, 400)
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--show-toplevel",
            cwd=os.path.dirname(host),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return JSONResponse({"error": "Not in a git repo"}, 400)
        repo = out.decode().strip()
        rel = os.path.relpath(host, repo).replace("\\", "/")
        return JSONResponse({"lines": await Git(repo).blame(rel)})

    async def branches(self, path: str = Query(...)):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"current": await git.current_branch(), "all": await git.branches()})

    async def log(self, path: str = Query(...), branch: str = Query(...),
                  limit: int = Query(15), skip: int = Query(0)):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"commits": await git.last_commits(branch, limit, skip)})

    async def history(self, path: str = Query(...), commit: str = Query(...), depth: int = Query(1)):
        git, err = self._git(path)
        if err: return err
        return JSONResponse({"files": await git.history(commit, depth)})

    async def _check_hash(self, git: Git, data: dict):
        sent = data.get("status_hash")
        if not sent:
            return None
        current = Git.status_hash(await git.status())
        if sent != current:
            return JSONResponse(
                {"error": "Status changed since you loaded this view — please refresh",
                 "status_hash": current}, 409,
            )
        return None

    async def stage(self, request: Request):
        data = await request.json()
        git, err = self._git(data["path"])
        if err: return err
        out, code = await (git.stage(data["file"]) if data.get("staged") else git.unstage(data["file"]))
        return JSONResponse({"status": "ok" if code == 0 else "error", "output": out})

    async def checkout(self, request: Request):
        data = await request.json()
        git, err = self._git(data["path"])
        if err: return err
        out, code = await git.checkout_file(data["file"], data.get("old_file"))
        return JSONResponse({"status": "ok" if code == 0 else "error", "output": out})

    async def commit(self, request: Request):
        data = await request.json()
        git, err = self._git(data["path"])
        if err: return err
        if (mismatch := await self._check_hash(git, data)): return mismatch
        message = (data.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "Empty commit message"}, 400)
        out, code = await git.commit(message)
        return JSONResponse({"status": "ok" if code == 0 else "error", "output": out})

    async def amend(self, request: Request):
        data = await request.json()
        git, err = self._git(data["path"])
        if err: return err
        if (mismatch := await self._check_hash(git, data)): return mismatch
        out, code = await git.amend((data.get("message") or "").strip() or None)
        return JSONResponse({"status": "ok" if code == 0 else "error", "output": out})

    async def switch_branch(self, request: Request):
        data = await request.json()
        git, err = self._git(data["path"])
        if err: return err
        out, code = await git.switch_branch(data["branch"])
        return JSONResponse({"status": "ok" if code == 0 else "error", "output": out})
