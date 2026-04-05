"""AI tools for the Storyboard tab."""
from typing import Annotated

from agent import Skill, tool
from src.modes.movie_creator.project import Project
from src.modes.movie_creator.server import MovieServer

SHOT_SEPARATOR = "\n\n---\n\n"


class StoryboardSkill(Skill):
    """AI tools available in the Storyboard tab."""

    def __init__(self, project: Project, server: MovieServer):
        super().__init__()
        self.project = project
        self.server = server

    async def get_context_prompt(self, user_text: str = "") -> str:
        scene_id = self.server.active_scope.get("scene_id", "")
        current_scene = self.project.scenes.get(scene_id) if scene_id else None
        current_block = ""
        if current_scene:
            shots_dump = self.project.dump(current_scene.shots)
            current_block = (
                f"\n\nТЕКУЩАЯ ВЫБРАННАЯ СЦЕНА: id={current_scene.id}\n"
                f"КАДРЫ ЭТОЙ СЦЕНЫ:\n{shots_dump}"
            )
        return (
            "Ты — ассистент по раскадровке. Помогаешь разбивать сцены на кадры.\n"
            "Кадр (shot) — это один план: описание включает крупность, действие, камеру, диалог.\n\n"
            "Инструменты:\n"
            "• create_shot — один кадр\n"
            "• create_shots_bulk — сразу несколько кадров (предпочтительнее для разбивки сцены)\n"
            "• update_shot — изменить существующий\n\n"
            "По умолчанию работай с текущей выбранной сценой, но ты видишь все сцены "
            "и всех персонажей и можешь использовать эту информацию.\n\n"
            f"ВСЕ СЦЕНЫ:\n{self.project.dump(self.project.scenes)}\n\n"
            f"ВСЕ ПЕРСОНАЖИ:\n{self.project.dump(self.project.characters)}"
            f"{current_block}"
        )

    @tool("Создать один кадр в сцене. Пользователь сможет отредактировать и одобрить.")
    async def create_shot(
        self,
        scene_id: Annotated[str, "ID сцены, к которой относится кадр"],
        description: Annotated[str, "Описание кадра — крупность, действие, камера, диалог в одном тексте"],
    ) -> dict:
        return await self.server.edit(
            ["scenes", scene_id, "shots"],
            {"description": description},
            approval=True,
        )

    @tool(
        "Предложить сразу несколько кадров для сцены. Пользователь увидит все описания в одном окне, "
        "сможет их отредактировать и одобрить списком. Используй для первичной разбивки сцены на кадры."
    )
    async def create_shots_bulk(
        self,
        scene_id: Annotated[str, "ID сцены"],
        descriptions: Annotated[list[str], "Список описаний кадров по порядку"],
    ) -> dict:
        result = await self.server.request_approval("shots_bulk", {
            "scene_id": scene_id,
            "text": SHOT_SEPARATOR.join(descriptions),
        })
        if result.get("action") == "reject":
            return {"status": "rejected", "reason": result.get("reason", "")}
        text = result.get("data", {}).get("text", "")
        container, cls = self.project.resolve_path(["scenes", scene_id, "shots"])
        if container is None:
            return {"status": "error", "error": f"scene {scene_id} not found"}
        ids = []
        for desc in text.split(SHOT_SEPARATOR):
            desc = desc.strip()
            if desc:
                obj = self.project.create(container, cls, description=desc)
                ids.append(obj.id)
        await self.server.send_project()
        return {"status": "created", "ids": ids, "count": len(ids)}

    @tool("Обновить описание существующего кадра. Пользователь сможет отредактировать и одобрить.")
    async def update_shot(
        self,
        scene_id: Annotated[str, "ID сцены"],
        id: Annotated[str, "ID кадра"],
        description: Annotated[str, "Новое описание кадра"],
    ) -> dict:
        return await self.server.edit(
            ["scenes", scene_id, "shots", id],
            {"description": description},
            approval=True,
        )
