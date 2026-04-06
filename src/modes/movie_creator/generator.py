"""Media generation queue + worker. Handles image (and later video) generations
attached to any owner entity that has a `generations` dict slot."""
import asyncio
import base64
import logging
import os

import requests

from src.modes.movie_creator.project import Generation

log = logging.getLogger(__name__)


class Generator:
    """Background worker for image/video generation.

    Bound to a MovieServer so it can reach the project tree, save to disk,
    and broadcast updates to the UI without extra callback plumbing.
    """

    def __init__(self, server, api_key: str):
        self.server = server
        self.api_key = api_key
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    async def enqueue(self, owner, kind: str, prompt: str, media_type: str = "image") -> str:
        gen = Generation(
            id=self.server.project.allocate_id(),
            kind=kind,
            media_type=media_type,
            prompt=prompt,
        )
        owner.generations[gen.id] = gen
        await self.server.save()

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
        gen = owner.generations.get(gen_id)
        if not gen:
            return
        gen.status = "generating"
        await self.server.save()

        try:
            if gen.media_type == "image":
                data, ext = await self._gen_image(gen.prompt), "png"
            else:
                raise NotImplementedError(f"media_type={gen.media_type}")

            filename = f"gen_{gen_id}.{ext}"
            self.server.assets_dir.mkdir(parents=True, exist_ok=True)
            (self.server.assets_dir / filename).write_bytes(data)
            gen.file = filename
            gen.status = "done"
            # First successful generation becomes primary automatically
            if hasattr(owner, "primary_generation_id") and not owner.primary_generation_id:
                owner.primary_generation_id = gen.id
        except Exception as e:
            log.exception("[movie] generation failed")
            gen.status = "failed"
            gen.error = str(e)

        await self.server.save()

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
