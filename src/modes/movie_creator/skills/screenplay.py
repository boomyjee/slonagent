"""AI tools for the Screenplay tab."""
from typing import Annotated

from agent import Skill, tool
from src.modes.movie_creator.project import Project
from src.modes.movie_creator.server import MovieServer


class ScreenplaySkill(Skill):
    """AI tools available in the Screenplay tab."""

    def __init__(self, project: Project, server: MovieServer):
        super().__init__()
        self.project = project
        self.server = server

    async def get_context_prompt(self, user_text: str = "") -> str:
        scenes = self.project.ordered("scenes")
        if not scenes:
            ctx = "Проект пока пуст — нет ни одной сцены."
        else:
            parts = []
            for i, s in enumerate(scenes, 1):
                parts.append(
                    f"Сцена {i} (id={s.id}): {s.title or 'Без названия'}"
                    f"{' [' + s.location + ']' if s.location else ''}\n{s.text or '(пусто)'}"
                )
            ctx = "\n\n".join(parts)
        return (
            "Ты — ассистент-сценарист. Помогаешь пользователю работать со сценарием.\n"
            "Когда пользователь просит создать или изменить сцену — используй propose_scene.\n"
            "Когда пользователь просит изменить существующую — используй update_scene.\n\n"
            f"ТЕКУЩИЙ СЦЕНАРИЙ ({len(scenes)} сцен):\n{ctx}"
        )

    @tool("Создать новую сцену. Пользователь сможет отредактировать и одобрить.")
    async def create_scene(
        self,
        title: Annotated[str, "Название сцены"],
        location: Annotated[str, "Локация (напр. INT. КВАРТИРА - НОЧЬ)"] = "",
        text: Annotated[str, "Текст сцены (действие, диалоги)"] = "",
    ) -> dict:
        result = await self.server.request_approval("scene", {
            "title": title, "location": location, "text": text,
        })
        if result.get("action") == "reject":
            return {"status": "rejected", "reason": result.get("reason", "")}
        scene = self.project.create("scenes", **result.get("data", {}))
        await self.server.send_project()
        return {"status": "created", "scene_id": scene.id}

    @tool("Обновить существующую сцену по ID. Пользователь сможет отредактировать и одобрить.")
    async def update_scene(
        self,
        scene_id: Annotated[str, "ID сцены для обновления"],
        title: Annotated[str, "Новое название (пусто = не менять)"] = "",
        location: Annotated[str, "Новая локация (пусто = не менять)"] = "",
        text: Annotated[str, "Новый текст (пусто = не менять)"] = "",
    ) -> dict:
        scene = self.project.scenes.get(scene_id)
        if not scene:
            return {"error": f"Scene {scene_id} not found"}
        proposed = {
            "title": title or scene.title,
            "location": location or scene.location,
            "text": text or scene.text,
        }
        result = await self.server.request_approval("scene", proposed)
        if result.get("action") == "reject":
            return {"status": "rejected", "reason": result.get("reason", "")}
        self.project.update("scenes", scene_id, **result.get("data", {}))
        await self.server.send_project()
        return {"status": "updated", "scene_id": scene_id}
