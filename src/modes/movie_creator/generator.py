"""Media generation. Each generation runs as an independent asyncio task."""
import asyncio
import base64
import logging
import os

import av
import requests

from src.modes.movie_creator.project import Generation

log = logging.getLogger(__name__)

MODELS = {
    # Image
    "gemini-image": {"type": "image", "label": "Nano Banana 2"},
    "muapi-seedance2.0-character": {"type": "image", "label": "Seedance Character"},
    "evolink-seedream5.0": {"type": "image", "label": "Seedream 5.0 Lite", "handler": "evolink-image", "model_id": "doubao-seedream-5.0-lite"},
    "evolink-seedance2.0-reference": {"type": "video", "label": "Seedance 2.0 Reference", "handler": "evolink", "mode": "reference"},
    "evolink-seedance2.0-first-last": {"type": "video", "label": "Seedance 2.0 First-Last", "handler": "evolink", "mode": "first-last"},
}


class Generator:
    def __init__(self, movie, gemini_key: str, muapi_key: str = "", evolink_key: str = ""):
        self.movie = movie
        self.gemini_key = gemini_key
        self.muapi_key = muapi_key or os.getenv("MUAPI_API_KEY", "")
        self.evolink_key = evolink_key or os.getenv("EVOLINK_API_KEY", "")

    async def enqueue(self, owner, kind: str, prompt: str, model: str = "gemini-image",
                      references: list = None, duration: int = 5,
                      aspect_ratio: str = "16:9", resolution: str = "720p",
                      fast: bool = False) -> str:
        ref_files = [r.name for r in (references or []) if r.exists()]
        model_info = MODELS.get(model, {})
        media_type = model_info.get("type", "image")
        gen = Generation(
            id=self.movie.project.allocate_id(),
            kind=kind,
            media_type=media_type,
            model=model,
            prompt=prompt,
            references=ref_files,
            resolution=resolution,
            fast=fast,
        )
        owner.generations[gen.id] = gen
        await self.movie.save()
        asyncio.create_task(self._process(owner, gen, references or [],
                                          duration=duration, aspect_ratio=aspect_ratio))
        return gen.id

    async def _process(self, owner, gen, refs: list, duration: int = 5,
                       aspect_ratio: str = "16:9"):
        try:
            model_info = MODELS.get(gen.model, {})
            if gen.model == "gemini-image":
                data = await self._gemini_image(gen.prompt, refs)
            elif gen.model == "muapi-seedance2.0-character":
                data, character_id = await self._muapi_character(gen.prompt, refs)
                gen.character_id = character_id
            elif model_info.get("handler") == "evolink-image":
                data = await self._evolink_image(
                    model_info["model_id"], gen.prompt, refs,
                )
            elif model_info.get("handler") == "evolink":
                data = await self._evolink_video(
                    model_info["mode"], gen.prompt, refs,
                    duration=duration, aspect_ratio=aspect_ratio,
                    quality=gen.resolution or "720p",
                    fast=gen.fast,
                )
            else:
                raise NotImplementedError(f"Unknown model: {gen.model}")

            ext = "mp4" if gen.media_type == "video" else "png"
            filename = f"gen_{gen.id}.{ext}"
            self.movie.assets_dir.mkdir(parents=True, exist_ok=True)
            filepath = self.movie.assets_dir / filename
            filepath.write_bytes(data)
            if ext == "mp4":
                poster_name = f"gen_{gen.id}.poster.jpg"
                self._extract_poster(filepath, self.movie.assets_dir / poster_name)
                gen.poster = poster_name
            gen.file = filename
            gen.status = "done"
            if hasattr(owner, "primary_generation_id") and not owner.primary_generation_id:
                owner.primary_generation_id = gen.id
        except Exception as e:
            log.exception("[movie] generation failed")
            gen.status = "failed"
            gen.error = str(e)

        await self.movie.save()

    @staticmethod
    def _extract_poster(video_path, poster_path):
        """Extract first frame from mp4 and save as poster jpg."""
        try:
            with av.open(str(video_path)) as container:
                frame = next(container.decode(video=0))
                frame.to_image().save(str(poster_path), quality=85)
            log.info("[movie] poster saved: %s", poster_path.name)
        except Exception as e:
            log.warning("[movie] failed to extract poster from %s: %s", video_path.name, e)

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
        log.info("[movie] POST gemini generateContent, %d images, prompt=%s",
                 len(parts) - 1, prompt)
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

    async def _muapi_poll(self, request_id, timeout_minutes: int = 60) -> dict:
        headers = self._muapi_headers()
        poll_url = f"https://api.muapi.ai/api/v1/predictions/{request_id}/result"
        deadline = asyncio.get_event_loop().time() + timeout_minutes * 60
        while True:
            await asyncio.sleep(5)
            r = await asyncio.to_thread(requests.get, poll_url, headers=headers, timeout=30)
            data = r.json()
            detail = data.get("detail", {})
            if isinstance(detail, dict) and detail.get("status"):
                data = detail
            status = data.get("status", "")
            log.info("[movie] poll %s → %s", request_id, status or f"(raw: {data})")
            if status == "completed":
                return data
            elif status == "failed":
                raise RuntimeError(f"muapi failed: {data}")
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(f"muapi poll timeout ({timeout_minutes}min), last status: {status!r}")

    async def _muapi_upload(self, path) -> str:
        """Upload a local file to muapi CDN, return public URL."""
        filename = path.name
        resp = await asyncio.to_thread(
            requests.get,
            f"https://muapi.ai/api/app/get_file_upload_url?filename={filename}",
            headers={"x-api-key": self.muapi_key}, timeout=30,
        )
        data = resp.json()
        fields = data["fields"]
        mime = "image/png" if path.suffix == ".png" else "image/jpeg"
        await asyncio.to_thread(
            requests.post, data["url"],
            data=fields, files={"file": (filename, path.read_bytes(), mime)}, timeout=120,
        )
        return f"https://cdn.muapi.ai/{fields['key']}"

    async def _muapi_character(self, prompt: str, refs: list) -> tuple[bytes, str]:
        """Create character sheet, return (image_bytes, character_id)."""
        if not self.muapi_key:
            raise RuntimeError("MUAPI_API_KEY not set")
        images_list = []
        for ref_path in refs:
            if ref_path.exists():
                url = await self._muapi_upload(ref_path)
                images_list.append(url)
        payload = {"prompt": prompt}
        if images_list:
            payload["images_list"] = images_list
        api_url = "https://api.muapi.ai/api/v1/seedance-2-character"
        log.info("[movie] POST %s payload=%s", api_url, payload)
        resp = await asyncio.to_thread(
            requests.post, api_url,
            json=payload, headers=self._muapi_headers(), timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"muapi {resp.status_code}: {resp.text[:300]}")
        request_id = resp.json().get("request_id")
        if not request_id:
            raise RuntimeError(f"No request_id: {resp.text[:300]}")
        result = await self._muapi_poll(request_id)
        character_id = (result.get("outputs") or [request_id])[0]
        sheet_url = result.get("sheet_url")
        if not sheet_url:
            raise RuntimeError(f"No sheet_url in character result: {list(result.keys())}")
        img_resp = await asyncio.to_thread(requests.get, sheet_url, timeout=120)
        return img_resp.content, character_id

    # -- evolink.ai --

    async def _evolink_poll(self, task_id: str, timeout_minutes: int = 10) -> dict:
        headers = {"Authorization": f"Bearer {self.evolink_key}"}
        poll_url = f"https://api.evolink.ai/v1/tasks/{task_id}"
        deadline = asyncio.get_event_loop().time() + timeout_minutes * 60
        while True:
            await asyncio.sleep(5)
            try:
                r = await asyncio.to_thread(requests.get, poll_url, headers=headers, timeout=60)
                data = r.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                log.warning("[movie] poll evolink %s network error, retrying: %s", task_id, e)
                continue
            status = data.get("status", "")
            progress = data.get("progress", 0)
            log.info("[movie] poll evolink %s → %s (%d%%)", task_id, status, progress)
            if status == "completed":
                return data
            elif status == "failed":
                error = data.get("error", {})
                raise RuntimeError(f"evolink failed: {error.get('message', data)}")
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(f"evolink poll timeout ({timeout_minutes}min)")

    async def _evolink_upload(self, path) -> str:
        """Upload image to evolink CDN via base64, return public URL. Resizes if >10MB or >6000px."""
        from PIL import Image
        import io
        raw = path.read_bytes()
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        need_resize = w > 6000 or h > 6000 or len(raw) > 10_000_000
        if need_resize:
            img.thumbnail((6000, 6000), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            raw = buf.getvalue()
            mime = "image/jpeg"
            name = path.stem + ".jpg"
            log.info("[movie] evolink resize %s: %dx%d → %dx%d (%d KB)", path.name, w, h, img.size[0], img.size[1], len(raw) // 1024)
        else:
            mime = "image/png" if path.suffix == ".png" else "image/jpeg"
            name = path.name
        data = base64.b64encode(raw).decode()
        payload = {
            "base64_data": f"data:{mime};base64,{data}",
            "file_name": name,
        }
        resp = await asyncio.to_thread(
            requests.post,
            "https://files-api.evolink.ai/api/v1/files/upload/base64",
            json=payload,
            headers={"Authorization": f"Bearer {self.evolink_key}"},
            timeout=60,
        )
        result = resp.json()
        if not result.get("success"):
            raise RuntimeError(f"evolink upload failed: {result}")
        url = result["data"]["file_url"]
        log.info("[movie] evolink upload %s → %s", path.name, url)
        return url

    async def _evolink_generate(self, api_path: str, payload: dict, timeout: int = 120) -> bytes:
        """POST to evolink API, poll for result, download first output."""
        if not self.evolink_key:
            raise RuntimeError("EVOLINK_API_KEY not set")
        api_url = f"https://api.evolink.ai/v1/{api_path}"
        log.info("[movie] POST %s payload=%s", api_url, {k: v for k, v in payload.items() if k != "image_urls"})
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.evolink_key}"}
        resp = await asyncio.to_thread(requests.post, api_url, json=payload, headers=headers, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"evolink {resp.status_code}: {resp.text[:300]}")
        task_id = resp.json().get("id")
        if not task_id:
            raise RuntimeError(f"No task id: {resp.text[:300]}")
        log.info("[movie] evolink task_id=%s", task_id)
        result = await self._evolink_poll(task_id)
        results = result.get("results") or []
        if not results:
            raise RuntimeError("Completed but no results")
        dl = await asyncio.to_thread(requests.get, results[0], timeout=timeout)
        return dl.content

    async def _evolink_image(self, model_id: str, prompt: str, refs: list = None) -> bytes:
        payload = {"model": model_id, "prompt": prompt, "size": "16:9", "quality": "2K"}
        valid_refs = [r for r in (refs or []) if r.exists()]
        if valid_refs:
            payload["image_urls"] = [await self._evolink_upload(ref) for ref in valid_refs[:14]]
        return await self._evolink_generate("images/generations", payload)

    async def _evolink_video(self, mode: str, prompt: str, refs: list = None,
                              duration: int = 5, aspect_ratio: str = "16:9",
                              quality: str = "720p", fast: bool = False) -> bytes:
        valid_refs = [r for r in (refs or []) if r.exists()]
        if not valid_refs:
            base = "text-to-video"
        elif mode == "first-last":
            base = "image-to-video"
        else:
            base = "reference-to-video"
        model_id = f"seedance-2.0-{'fast-' if fast else ''}{base}"
        payload = {
            "model": model_id, "prompt": prompt, "duration": duration,
            "quality": quality, "aspect_ratio": aspect_ratio, "generate_audio": True,
        }
        if valid_refs:
            max_refs = 2 if mode == "first-last" else 9
            payload["image_urls"] = [await self._evolink_upload(ref) for ref in valid_refs[:max_refs]]
        return await self._evolink_generate("videos/generations", payload, timeout=300)

