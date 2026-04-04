"""AI tools for the Characters tab."""
from typing import Annotated

from agent import Skill, tool
from src.modes.movie_creator.project import Project
from src.modes.movie_creator.server import MovieServer


class CharactersSkill(Skill):
    """AI tools available in the Characters tab."""

    def __init__(self, project: Project, server: MovieServer):
        super().__init__()
        self.project = project
        self.server = server

    async def get_context_prompt(self, user_text: str = "") -> str:
        chars = self.project.characters_ordered()
        scenes = self.project.scenes_ordered()

        if not chars:
            chars_ctx = "Персонажей пока нет."
        else:
            parts = []
            for c in chars:
                parts.append(
                    f"- {c.name} (id={c.id}): {c.description or '(нет описания)'}"
                    f"\n  Внешность: {c.appearance or '(не описана)'}"
                )
            chars_ctx = "\n".join(parts)

        if not scenes:
            scenes_ctx = "Сцен пока нет."
        else:
            parts = []
            for i, s in enumerate(scenes, 1):
                parts.append(
                    f"Сцена {i} (id={s.id}): {s.title or 'Без названия'}"
                    f"{' [' + s.location + ']' if s.location else ''}\n{s.text or '(пусто)'}"
                )
            scenes_ctx = "\n\n".join(parts)

        return (
            "Ты — ассистент по персонажам фильма.\n"
            "Помогаешь создавать и редактировать персонажей.\n"
            "Когда пользователь просит создать — используй create_character.\n"
            "Когда просит изменить — используй update_character.\n"
            "Каждый вызов покажет пользователю форму для одобрения.\n\n"
            f"ПЕРСОНАЖИ ({len(chars)}):\n{chars_ctx}\n\n"
            f"СЦЕНАРИЙ ({len(scenes)} сцен):\n{scenes_ctx}"
        )

    @tool("Создать нового персонажа. Пользователь сможет отредактировать и одобрить.")
    async def create_character(
        self,
        name: Annotated[str, "Имя персонажа"],
        description: Annotated[str, "Описание (роль, характер, мотивация)"] = "",
        appearance: Annotated[str, "Внешность (возраст, рост, волосы, одежда)"] = "",
    ) -> dict:
        result = await self.server.request_approval("character", {
            "name": name, "description": description, "appearance": appearance,
        })
        if result.get("action") == "reject":
            return {"status": "rejected", "reason": result.get("reason", "")}
        data = result.get("data", {})
        char = self.project.create_character(
            name=data.get("name", name),
            description=data.get("description", description),
            appearance=data.get("appearance", appearance),
        )
        await self.server.send_event("project_updated",
                                     project=self.project.to_dict())
        return {"status": "created", "id": char.id, "name": char.name}

    @tool("Обновить существующего персонажа по ID. Пользователь сможет отредактировать и одобрить.")
    async def update_character(
        self,
        character_id: Annotated[str, "ID персонажа"],
        name: Annotated[str, "Новое имя (пусто = не менять)"] = "",
        description: Annotated[str, "Новое описание (пусто = не менять)"] = "",
        appearance: Annotated[str, "Новая внешность (пусто = не менять)"] = "",
    ) -> dict:
        char = self.project.characters.get(character_id)
        if not char:
            return {"error": f"Character {character_id} not found"}
        proposed = {
            "name": name or char.name,
            "description": description or char.description,
            "appearance": appearance or char.appearance,
        }
        result = await self.server.request_approval("character", proposed)
        if result.get("action") == "reject":
            return {"status": "rejected", "reason": result.get("reason", "")}
        data = result.get("data", {})
        self.project.update_character(character_id, **data)
        await self.server.send_event("project_updated",
                                     project=self.project.to_dict())
        return {"status": "updated", "character_id": character_id}
