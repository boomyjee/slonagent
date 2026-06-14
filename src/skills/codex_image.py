from agent import Skill, Agent, tool
from typing import Annotated
import base64
import contextlib
import json
import logging
import os

from src.transport.base import BaseTransport

log = logging.getLogger(__name__)


class CodexImageSkill(Skill):
    """Генерация изображений через image-модель OpenAI, проксированную codex'ом.

    Доступна ЛЮБОМУ агенту (не только codex-форку): внутри поднимает
    эфемерный one-shot codex-ход с включённым нативным image_generation.
    Авторизацию/рефреш токена/эндпоинт целиком берёт на себя codex
    app-server. Нативный хендлер кладёт результат генерации в память
    эфемерного агента флагнутым (_codex_image_generation) tool-турном с
    `saved_path` — мы оттуда забираем путь и отдаём картинку как обычный
    tool-результат (картинка в чат + `_parts` в контекст вызывающей модели).
    """

    @tool("Сгенерировать изображение по текстовому описанию через image-модель OpenAI (codex).")
    async def generate_image(
        self,
        prompt: Annotated[str, "Подробное описание того, что должно быть на картинке."],
        filename: Annotated[str, "Имя файла или путь внутри контейнера (например '/workspace/art.png')."] = "generated_art.png",
    ) -> dict:
        gen = Agent(
            id="", model_name="", backend="codex",
            backend_params={"enable_features": ["image_generation"]},
            max_iterations=4, transport=BaseTransport(),
        )
        saved_path, revised = None, ""
        try:
            await gen.memory.add_turn({"role": "user", "content": (
                "Сгенерируй изображение инструментом image_generation. "
                f"Описание: {prompt}\nСделай это один раз, ничего не уточняя."
            )})
            # llm() ВОЗВРАЩАЕТ turns (в gen.memory их кладёт agent-loop, которого
            # при прямом вызове нет). Нативный хендлер положил результат генерации
            # флагнутым tool-турном с saved_path внутри.
            turns = await gen.llm()
            for t in (turns if isinstance(turns, list) else []):
                if t.get("_codex_image_generation") and t.get("role") == "tool":
                    data = json.loads(t.get("content") or "{}")
                    saved_path = data.get("saved_path") or saved_path
                    revised = data.get("revised_prompt") or revised
        except Exception as e:
            log.warning("[codex_image] generation failed: %s", e, exc_info=True)
            return {"error": f"Ошибка генерации: {e}"}
        finally:
            with contextlib.suppress(Exception):
                await gen.close()

        if not saved_path or not os.path.exists(saved_path):
            return {"error": "codex не вернул изображение"}

        with open(saved_path, "rb") as f:
            img = f.read()

        # Кладём в workspace вызывающего агента (как NanoBanana). Нет sandbox —
        # отдаём из codex'ова generated_images как есть.
        container_path = filename if filename.startswith("/") else f"/workspace/{filename}"
        sandbox = self.agent.sandbox
        host_path = sandbox.resolve_path(container_path) if sandbox else None
        if host_path:
            os.makedirs(os.path.dirname(host_path), exist_ok=True)
            with open(host_path, "wb") as f:
                f.write(img)
            deliver, shown = host_path, container_path
        else:
            deliver, shown = saved_path, saved_path

        with contextlib.suppress(Exception):
            await self.agent.transport.send_images([deliver])

        # Подчищаем исходник codex'а (~/.codex/generated_images/<thread>/…png) —
        # данные уже в workspace + в _parts. Заодно сносим опустевшую папку треда.
        if host_path:
            with contextlib.suppress(OSError):
                os.remove(saved_path)
                os.rmdir(os.path.dirname(saved_path))

        b64 = base64.b64encode(img).decode()
        return {
            "status": "success",
            "message": f"Изображение сгенерировано (codex / OpenAI image): {revised or prompt}",
            "container_path": shown,
            "_parts": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}],
        }
