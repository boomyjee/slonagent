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
        scenes = self.project.scenes_ordered()
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

    @tool("Предложить новую сцену пользователю. Откроется окно редактирования, где он сможет поправить и сохранить.")
    async def propose_scene(
        self,
        title: Annotated[str, "Название сцены"],
        location: Annotated[str, "Локация (напр. INT. КВАРТИРА - НОЧЬ)"] = "",
        text: Annotated[str, "Текст сцены (действие, диалоги)"] = "",
    ) -> dict:
        await self.server.send_scene_proposal({
            "title": title, "location": location, "text": text,
        })
        return {"status": "proposed", "title": title}

    @tool("Обновить существующую сцену по ID")
    async def update_scene(
        self,
        scene_id: Annotated[str, "ID сцены для обновления"],
        title: Annotated[str, "Новое название (пусто = не менять)"] = "",
        location: Annotated[str, "Новая локация (пусто = не менять)"] = "",
        text: Annotated[str, "Новый текст (пусто = не менять)"] = "",
    ) -> dict:
        fields = {}
        if title:
            fields["title"] = title
        if location:
            fields["location"] = location
        if text:
            fields["text"] = text
        if not fields:
            return {"error": "Nothing to update"}
        scene = self.project.update_scene(scene_id, **fields)
        if not scene:
            return {"error": f"Scene {scene_id} not found"}
        await self.server.send_event("project_updated",
                                     project=self.project.to_dict())
        return {"status": "updated", "scene_id": scene_id}
