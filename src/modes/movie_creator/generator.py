"""Media generation queue + worker. Handles image (and later video) generations
attached to any owner entity that has a `generations` slot (list of dicts)."""
import asyncio
import base64
import logging
import os

import requests

from src.modes.movie_creator.project import Generation, Project

log = logging.getLogger(__name__)


class Generator:
    def __init__(self, project: Project, api_key: str, notify):
        self.project = project
        self.api_key = api_key
        self.notify = notify  # async callable — broadcasts project state to UI
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    async def enqueue(self, owner, kind: str, prompt: str, media_type: str = "image") -> str:
        """Append a queued Generation to owner.generations and schedule processing.

        owner — any Entity with a `generations` list slot.
        kind  — semantic role within owner (portrait, extra, location, ...).
        """
        gen = Generation(
            id=self.project.next_id(),
            kind=kind,
            media_type=media_type,
            prompt=prompt,
        )
        owner.generations.append(gen)
        self.project.save()
        await self.notify()

        await self._queue.put((owner, gen.id))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        return gen.id

    async def _run(self):
        while True:
            owner, gen_id = await self._queue.get()
            try:
                await self._process(owner, gen_id)
            except Exception:
                log.exception("[movie] generator worker error")

    async def _process(self, owner, gen_id: str):
        gen = next((g for g in owner.generations if g.id == gen_id), None)
        if not gen:
            return
        gen.status = "generating"
        self.project.save()
        await self.notify()

        try:
            if gen.media_type == "image":
                data, ext = await self._gen_image(gen.prompt), "png"
            else:
                raise NotImplementedError(f"media_type={gen.media_type}")

            filename = f"gen_{gen_id}.{ext}"
            self.project.assets_dir.mkdir(parents=True, exist_ok=True)
            (self.project.assets_dir / filename).write_bytes(data)
            gen.file = filename
            gen.status = "done"
            # First successful portrait becomes primary automatically
            if gen.kind == "portrait" and not getattr(owner, "image", ""):
                owner.image = filename
        except Exception as e:
            log.exception("[movie] generation failed")
            gen.status = "failed"
            gen.error = str(e)

        self.project.save()
        await self.notify()

    async def _gen_image(self, prompt: str) -> bytes:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash-image:generateContent?key={self.api_key}"
        )
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = await asyncio.to_thread(
            requests.post, url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            proxies=proxies, timeout=300,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"API {resp.status_code}: {resp.text[:200]}")
        for cand in resp.json().get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "inlineData" in part:
                    return base64.b64decode(part["inlineData"]["data"])
        raise RuntimeError("No image in API response")
