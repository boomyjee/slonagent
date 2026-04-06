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
    "seedance-character": {"label": "Seedance Character"},
}


class Generator:
    def __init__(self, server, gemini_key: str, muapi_key: str = ""):
        self.server = server
        self.gemini_key = gemini_key
        self.muapi_key = muapi_key or os.getenv("MUAPI_API_KEY", "")

    async def enqueue(self, owner, kind: str, prompt: str, model: str = "gemini-image",
                      references: list = None) -> str:
        ref_files = [r.name for r in (references or []) if r.exists()]
        gen = Generation(
            id=self.server.project.allocate_id(),
            kind=kind,
            media_type="image",
            model=model,
            prompt=prompt,
            references=ref_files,
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
                data = await self._muapi_image("seedream-5.0", gen.prompt)
            elif gen.model == "seedance-character":
                data, character_id = await self._muapi_character(gen.prompt, refs)
                gen.character_id = character_id
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
        body = resp.json()
        for cand in body.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "inlineData" in part:
                    return base64.b64decode(part["inlineData"]["data"])
        # Log what we got instead
        text_parts = []
        for cand in body.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "text" in part:
                    text_parts.append(part["text"])
        log.warning("[movie] Gemini returned text instead of image: %s", " ".join(text_parts)[:300])
        raise RuntimeError("No image in Gemini response")

    # -- muapi.ai --

    def _muapi_headers(self):
        return {"Content-Type": "application/json", "x-api-key": self.muapi_key}

    async def _muapi_poll(self, request_id) -> dict:
        headers = self._muapi_headers()
        poll_url = f"https://api.muapi.ai/api/v1/predictions/{request_id}/result"
        while True:
            await asyncio.sleep(5)
            r = await asyncio.to_thread(requests.get, poll_url, headers=headers, timeout=30)
            data = r.json()
            status = data.get("status", "")
            if status == "completed":
                return data
            elif status == "failed":
                raise RuntimeError(f"muapi failed: {data.get('error', 'unknown')}")

    async def _muapi_character(self, prompt: str, refs: list) -> tuple[bytes, str]:
        """Create character sheet, return (image_bytes, character_id)."""
        if not self.muapi_key:
            raise RuntimeError("MUAPI_API_KEY not set")
        images_list = []
        for ref_path in refs:
            if ref_path.exists():
                mime = "image/png" if ref_path.suffix == ".png" else "image/jpeg"
                data = base64.b64encode(ref_path.read_bytes()).decode()
                images_list.append(f"data:{mime};base64,{data}")
        payload = {"prompt": prompt}
        if images_list:
            payload["images_list"] = images_list
        resp = await asyncio.to_thread(
            requests.post,
            "https://api.muapi.ai/api/v1/seedance-2-character",
            json=payload, headers=self._muapi_headers(), timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"muapi {resp.status_code}: {resp.text[:300]}")
        request_id = resp.json().get("request_id")
        if not request_id:
            raise RuntimeError(f"No request_id: {resp.text[:300]}")
        result = await self._muapi_poll(request_id)
        outputs = result.get("outputs") or []
        if not outputs:
            raise RuntimeError("Completed but no outputs")
        img_resp = await asyncio.to_thread(requests.get, outputs[0], timeout=120)
        return img_resp.content, request_id

    async def _muapi_image(self, endpoint: str, prompt: str) -> bytes:
        """Submit to muapi.ai, poll for result, download image."""
        if not self.muapi_key:
            raise RuntimeError("MUAPI_API_KEY not set")

        resp = await asyncio.to_thread(
            requests.post,
            f"https://api.muapi.ai/api/v1/{endpoint}",
            json={"prompt": prompt, "aspect_ratio": "16:9", "quality": "high"},
            headers=self._muapi_headers(), timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"muapi {resp.status_code}: {resp.text[:300]}")

        request_id = resp.json().get("request_id")
        if not request_id:
            raise RuntimeError(f"No request_id: {resp.text[:300]}")

        result = await self._muapi_poll(request_id)
        outputs = result.get("outputs") or []
        if not outputs:
            raise RuntimeError("Completed but no outputs")
        img_resp = await asyncio.to_thread(requests.get, outputs[0], timeout=120)
        return img_resp.content
