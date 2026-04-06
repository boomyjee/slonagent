"""Media generation. Each generation runs as an independent asyncio task."""
import asyncio
import base64
import logging
import os

import requests

from src.modes.movie_creator.project import Generation

log = logging.getLogger(__name__)

MODELS = {
    "gemini-image": {"label": "Nano Banana 2"},
    "seedream-v5": {"label": "Seedream 5.0"},
}


class Generator:
    def __init__(self, server, gemini_key: str, muapi_key: str = ""):
        self.server = server
        self.gemini_key = gemini_key
        self.muapi_key = muapi_key or os.getenv("MUAPI_API_KEY", "")

    async def enqueue(self, owner, kind: str, prompt: str, model: str = "gemini-image",
                      references: list = None) -> str:
        gen = Generation(
            id=self.server.project.allocate_id(),
            kind=kind,
            media_type="image",
            model=model,
            prompt=prompt,
        )
        owner.generations[gen.id] = gen
        await self.server.save()
        asyncio.create_task(self._process(owner, gen, references or []))
        return gen.id

    async def _process(self, owner, gen, refs: list):
        try:
            if gen.model == "gemini-image":
                data = await self._gemini_image(gen.prompt, refs)
            elif gen.model == "seedream-v5":
                data = await self._muapi_image("bytedance-seedream-v5.0", gen.prompt)
            else:
                raise NotImplementedError(f"Unknown model: {gen.model}")

            filename = f"gen_{gen.id}.png"
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

    # -- Gemini --

    async def _gemini_image(self, prompt: str, refs: list) -> bytes:
        parts = []
        for ref_path in refs:
            if ref_path.exists():
                mime = "image/png" if ref_path.suffix == ".png" else "image/jpeg"
                img_data = base64.b64encode(ref_path.read_bytes()).decode()
                parts.append({"inlineData": {"mimeType": mime, "data": img_data}})
        parts.append({"text": prompt})

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-3.1-flash-image-preview:generateContent?key={self.gemini_key}"
        )
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = await asyncio.to_thread(
            requests.post, url,
            json={"contents": [{"parts": parts}]},
            proxies=proxies, timeout=300,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini API {resp.status_code}: {resp.text[:200]}")
        for cand in resp.json().get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "inlineData" in part:
                    return base64.b64decode(part["inlineData"]["data"])
        raise RuntimeError("No image in Gemini response")

    # -- muapi.ai --

    async def _muapi_image(self, endpoint: str, prompt: str) -> bytes:
        """Submit to muapi.ai, poll for result, download image."""
        if not self.muapi_key:
            raise RuntimeError("MUAPI_API_KEY not set")

        headers = {"Content-Type": "application/json", "x-api-key": self.muapi_key}
        resp = await asyncio.to_thread(
            requests.post,
            f"https://api.muapi.ai/api/v1/{endpoint}",
            json={"prompt": prompt, "aspect_ratio": "16:9", "quality": "high"},
            headers=headers, timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"muapi {resp.status_code}: {resp.text[:300]}")

        request_id = resp.json().get("request_id")
        if not request_id:
            raise RuntimeError(f"No request_id: {resp.text[:300]}")

        poll_url = f"https://api.muapi.ai/api/v1/predictions/{request_id}/result"
        for _ in range(120):
            await asyncio.sleep(5)
            r = await asyncio.to_thread(requests.get, poll_url, headers=headers, timeout=30)
            data = r.json()
            status = data.get("status", "")
            if status == "completed":
                outputs = data.get("outputs") or []
                if not outputs:
                    raise RuntimeError("Completed but no outputs")
                img_resp = await asyncio.to_thread(requests.get, outputs[0], timeout=120)
                return img_resp.content
            elif status == "failed":
                raise RuntimeError(f"muapi failed: {data.get('error', 'unknown')}")

        raise RuntimeError("muapi generation timed out")
