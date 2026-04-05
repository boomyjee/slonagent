"""AI tools for the Characters tab."""
from typing import Annotated

from agent import Skill, tool
from src.modes.movie_creator.project import Project, dump
from src.modes.movie_creator.server import MovieServer


class CharactersSkill(Skill):
    """AI tools available in the Characters tab."""

    def __init__(self, project: Project, server: MovieServer):
        super().__init__()
        self.project = project
        self.server = server

    async def get_context_prompt(self, user_text: str = "") -> str:
        return (
            "Ты — ассистент по персонажам фильма.\n"
            "Помогаешь создавать и редактировать персонажей.\n"
            "Когда пользователь просит создать — используй create_character.\n"
            "Когда просит изменить — используй update_character.\n"
            "Каждый вызов покажет пользователю форму для одобрения.\n\n"
            f"ПЕРСОНАЖИ:\n{dump(self.project.characters)}\n\n"
            f"СЦЕНАРИЙ:\n{dump(self.project.scenes)}"
        )

    @tool("Создать нового персонажа. Пользователь сможет отредактировать и одобрить.")
    async def create_character(
        self,
        name: Annotated[str, "Имя персонажа"],
        description: Annotated[str, "Описание (роль, характер, мотивация)"] = "",
        appearance: Annotated[str, "Внешность (возраст, рост, волосы, одежда)"] = "",
    ) -> dict:
        return await self.server.edit(
            ["characters"],
            {"name": name, "description": description, "appearance": appearance},
            approval=True,
        )

    @tool("Обновить существующего персонажа по ID. Пользователь сможет отредактировать и одобрить.")
    async def update_character(
        self,
        id: Annotated[str, "ID персонажа"],
        name: Annotated[str, "Новое имя (пусто = не менять)"] = "",
        description: Annotated[str, "Новое описание (пусто = не менять)"] = "",
        appearance: Annotated[str, "Новая внешность (пусто = не менять)"] = "",
    ) -> dict:
        return await self.server.edit(
            ["characters", id],
            {"name": name, "description": description, "appearance": appearance},
            approval=True,
        )

    @tool("Сгенерировать портрет персонажа. Передай полный промпт для image-модели. "
          "Генерация добавится в очередь — результат появится в галерее персонажа.")
    async def generate_portrait(
        self,
        character_id: Annotated[str, "ID персонажа"],
        prompt: Annotated[str, "Полный промпт для генерации (англ., описание лица/одежды/освещения)"],
    ) -> dict:
        char = self.project.characters.get(character_id)
        if not char:
            return {"error": f"Character {character_id} not found"}
        result = await self.server.request_approval("portrait", {
            "character_id": character_id,
            "character_name": char.name,
            "prompt": prompt,
        })
        if result.get("action") == "reject":
            return {"status": "rejected", "reason": result.get("reason", "")}
        data = result.get("data", {})
        gen_id = await self.server.generator.enqueue(
            char, "portrait", data.get("prompt", prompt))
        return {"status": "queued", "generation_id": gen_id}
