"""AI tools for the Storyboard tab."""
from typing import Annotated

from agent import Skill, tool
from src.modes.movie_creator.project import dump
from src.modes.movie_creator.server import MovieServer

SHOT_SEPARATOR = "\n\n---\n\n"


class StoryboardSkill(Skill):
    """AI tools available in the Storyboard tab."""

    def __init__(self, server: MovieServer):
        super().__init__()
        self.server = server

    async def get_context_prompt(self, user_text: str = "") -> str:
        project = self.server.project
        scene_id = self.server.selected.get("scenes", "")
        current_scene = project.scenes.get(scene_id) if scene_id else None
        current_block = ""
        if current_scene:
            current_block = (
                f"\n\nТЕКУЩАЯ ВЫБРАННАЯ СЦЕНА: id={current_scene.id}\n"
                f"КАДРЫ ЭТОЙ СЦЕНЫ:\n{dump(current_scene.shots)}"
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
            f"ВСЕ СЦЕНЫ:\n{dump(project.scenes)}\n\n"
            f"ВСЕ ПЕРСОНАЖИ:\n{dump(project.characters)}"
            f"{current_block}"
        )

    @tool("Создать один кадр в сцене. Пользователь сможет отредактировать и одобрить.")
    async def create_shot(
        self,
        scene_id: Annotated[str, "ID сцены, к которой относится кадр"],
        description: Annotated[str, "Описание кадра — крупность, действие, камера, диалог в одном тексте"],
    ) -> dict:
        return await self.server.edit(
            ["scenes", scene_id, "shots"], locals(), approval_kind="create_shot",
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
        result = await self.server.send_approval("create_shots_bulk", {
            "scene_id": scene_id,
            "text": SHOT_SEPARATOR.join(descriptions),
        })
        if result.get("action") == "reject":
            return {"status": "rejected", "reason": result.get("reason", "")}
        text = result.get("data", {}).get("text", "")
        shots_path = ["scenes", scene_id, "shots"]
        if not isinstance(self.server.project.resolve(shots_path), dict):
            return {"status": "error", "error": f"scene {scene_id} not found"}
        ids = []
        for desc in text.split(SHOT_SEPARATOR):
            desc = desc.strip()
            if desc:
                eid = self.server.project.create(shots_path, {"description": desc})
                if eid:
                    ids.append(eid)
        await self.server.save()
        return {"status": "created", "ids": ids, "count": len(ids)}

    @tool("Обновить описание существующего кадра. Пользователь сможет отредактировать и одобрить.")
    async def update_shot(
        self,
        scene_id: Annotated[str, "ID сцены"],
        id: Annotated[str, "ID кадра"],
        description: Annotated[str, "Новое описание кадра"],
    ) -> dict:
        return await self.server.edit(
            ["scenes", scene_id, "shots", id], locals(), approval_kind="update_shot",
        )
