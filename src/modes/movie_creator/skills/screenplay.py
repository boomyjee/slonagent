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
        return (
            "Ты — ассистент-сценарист. Помогаешь пользователю работать со сценарием.\n"
            "Когда пользователь просит создать сцену — используй create_scene.\n"
            "Когда пользователь просит изменить существующую — используй update_scene.\n\n"
            f"СЦЕНАРИЙ:\n{self.project.dump(self.project.scenes)}"
        )

    @tool("Создать новую сцену. Пользователь сможет отредактировать и одобрить.")
    async def create_scene(
        self,
        title: Annotated[str, "Название сцены"],
        location: Annotated[str, "Локация (напр. INT. КВАРТИРА - НОЧЬ)"] = "",
        text: Annotated[str, "Текст сцены (действие, диалоги)"] = "",
    ) -> dict:
        return await self.server.edit(
            ["scenes"],
            {"title": title, "location": location, "text": text},
            approval=True,
        )

    @tool("Обновить существующую сцену по ID. Пользователь сможет отредактировать и одобрить.")
    async def update_scene(
        self,
        id: Annotated[str, "ID сцены"],
        title: Annotated[str, "Новое название (пусто = не менять)"] = "",
        location: Annotated[str, "Новая локация (пусто = не менять)"] = "",
        text: Annotated[str, "Новый текст (пусто = не менять)"] = "",
    ) -> dict:
        return await self.server.edit(
            ["scenes", id],
            {"title": title, "location": location, "text": text},
            approval=True,
        )
