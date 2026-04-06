"""Media generation. Each generation runs as an independent asyncio task."""
import asyncio
import base64
import logging
import os

import requests

from src.modes.movie_creator.project import Generation

log = logging.getLogger(__name__)


class Generator:
    def __init__(self, server, api_key: str):
        self.server = server
        self.api_key = api_key

    async def enqueue(self, owner, kind: str, prompt: str, media_type: str = "image", references: list = None) -> str:
        gen = Generation(
            id=self.server.project.allocate_id(),
            kind=kind,
            media_type=media_type,
            prompt=prompt,
        )
        owner.generations[gen.id] = gen
        await self.server.save()
        asyncio.create_task(self._process(owner, gen, references or []))
        return gen.id

    async def _process(self, owner, gen, refs: list):
        try:
            if gen.media_type == "image":
                data, ext = await self._gen_image(gen.prompt, refs), "png"
            else:
                raise NotImplementedError(f"media_type={gen.media_type}")

            filename = f"gen_{gen.id}.{ext}"
            self.server.assets_dir.mkdir(parents=True, exist_ok=True)
            (self.server.assets_dir / filename).write_bytes(data)
            gen.file = filename
            gen.status = "done"
            if hasattr(owner, "primary_generation_id") and not owner.primary_generation_id:
                owner.primary_generation_id = gen.id
        except Exception as e:
            log.exception("[movie] generation failed")
            gen.status = "failed"
            gen.error = str(e)

        await self.server.save()

    async def _gen_image(self, prompt: str, refs: list) -> bytes:
        parts = []
        for ref_path in refs:
            if ref_path.exists():
                mime = "image/png" if ref_path.suffix == ".png" else "image/jpeg"
                img_data = base64.b64encode(ref_path.read_bytes()).decode()
                parts.append({"inlineData": {"mimeType": mime, "data": img_data}})
        parts.append({"text": prompt})

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-3.1-flash-image-preview:generateContent?key={self.api_key}"
        )
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = await asyncio.to_thread(
            requests.post, url,
            json={"contents": [{"parts": parts}]},
            proxies=proxies, timeout=300,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"API {resp.status_code}: {resp.text[:200]}")
        for cand in resp.json().get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "inlineData" in part:
                    return base64.b64decode(part["inlineData"]["data"])
        raise RuntimeError("No image in API response")
