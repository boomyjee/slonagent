from agent import Skill, tool
from typing import Annotated, Literal
import os
import base64
import asyncio
import requests
from google.genai import types

class NanoBananaSkill(Skill):
    def __init__(self, api_key: str):
        self.api_key = api_key
        super().__init__()

    @tool("Сгенерировать изображение по текстовому описанию с помощью одной из моделей Nano Banana.")
    async def generate_image(
        self,
        prompt: Annotated[str, "Подробное описание того, что должно быть на картинке."],
        filename: Annotated[str, "Имя файла или путь внутри контейнера (например, '/workspace/art.png')."] = "generated_art.png",
        model: Annotated[Literal["nano_banana", "nano_banana_2", "nano_banana_pro"], "Выбор модели: nano_banana (2.5 Flash - быстро), nano_banana_2 (3.1 Flash - качественно), nano_banana_pro (3 Pro - текст и логика)."] = "nano_banana",
        images: Annotated[list[str], "Список путей к входным изображениям внутри контейнера (например, ['/workspace/photo.jpg']). Используется для редактирования или стилизации существующих картинок."] = None,
    ) -> dict:
        """Генерация изображений через официальный API Google Gemini (семейство Nano Banana)."""

        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")

        model_map = {
            "nano_banana": "gemini-2.5-flash-image",
            "nano_banana_2": "gemini-3.1-flash-image-preview",
            "nano_banana_pro": "gemini-3-pro-image-preview"
        }
        model_id = model_map[model]

        container_path = filename if filename.startswith('/') else f"/workspace/{filename}"
        sandbox = next((s for s in self.agent.skills if s.__class__.__name__ == 'SandboxSkill'), None)
        host_path = sandbox.resolve_path(container_path) if sandbox else None
        if host_path is None:
            return {"error": f"Доступ запрещён: {container_path}"}

        dir_path = os.path.dirname(host_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        parts: list[dict] = []
        if images:
            for img_container_path in images:
                img_container_path = img_container_path if img_container_path.startswith('/') else f"/workspace/{img_container_path}"
                img_host_path = sandbox.resolve_path(img_container_path) if sandbox else None
                if img_host_path is None:
                    return {"error": f"Доступ запрещён к входному изображению: {img_container_path}"}
                if not os.path.exists(img_host_path):
                    return {"error": f"Входное изображение не найдено: {img_container_path}"}
                ext = os.path.splitext(img_host_path)[1].lower()
                mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
                mime_type = mime_map.get(ext, "image/png")
                with open(img_host_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("utf-8")
                parts.append({"inline_data": {"mime_type": mime_type, "data": encoded}})
        parts.append({"text": prompt})

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={self.api_key}"
        payload = {"contents": [{"parts": parts}]}
        proxies = {"http": proxy, "https": proxy} if proxy else None

        try:
            response = await asyncio.to_thread(requests.post, url, json=payload, proxies=proxies, timeout=90)
            if response.status_code != 200:
                return {"error": f"API Error {response.status_code}: {response.text}"}

            data = response.json()

            for candidate in data.get('candidates', []):
                for part in candidate.get('content', {}).get('parts', []):
                    if 'inlineData' in part:
                        mime_type = part['inlineData'].get('mimeType', 'image/png')
                        img_data = base64.b64decode(part['inlineData']['data'])

                        with open(host_path, "wb") as f:
                            f.write(img_data)

                        message_extra = ''
                        transport_skill = getattr(self.agent.transport, "_skill", None)
                        if transport_skill and transport_skill.send_images:
                            await transport_skill.send_images([container_path])
                            message_extra = " и отправлено пользователю"


                        return {
                            "status": "success",
                            "message": f"Изображение успешно сгенерировано моделью {model}{message_extra}",
                            "container_path": container_path,
                            "model_used": model_id,
                            "_parts": [types.Part.from_bytes(data=img_data, mime_type=mime_type)],
                        }

            return {"error": "Изображение не найдено в ответе API.", "details": str(data)[:500]}

        except Exception as e:
            return {"error": f"Произошла ошибка при генерации: {str(e)}"}
