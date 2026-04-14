"""Movie Creator mode — AI-assisted short film production pipeline.

Spawns a sub-agent whose transport is a MultiTransport fanning out to both the
parent's transport (Telegram etc.) and a `MovieTransport` (web editor). The
MovieTransport owns project state, assets and the generator; tab switches in
the UI rotate the active per-tab skill on the sub-agent so the AI's toolset
matches what the user is looking at.
"""
import asyncio
import logging
from pathlib import Path
from typing import Annotated

from agent import Skill, bypass, tool
from src.agent.agent import stoppable
from src.modes.movie_creator.generator import Generator
from src.modes.movie_creator.skills.characters import CharactersSkill
from src.modes.movie_creator.skills.screenplay import ScreenplaySkill
from src.modes.movie_creator.skills.storyboard import StoryboardSkill
from src.modes.movie_creator.transport import MovieTransport
from src.transport.multi import MultiTransport

log = logging.getLogger(__name__)


class _MovieControl(Skill):
    """Sub-agent-side /exit bypass so the user can leave Movie Creator."""

    def __init__(self):
        super().__init__()
        self.done = asyncio.Event()

    @bypass("exit", "Выйти из Movie Creator", standalone=True)
    async def exit_cmd(self, args: str):
        self.done.set()


class MovieCreatorSkill(Skill):
    """Entry point skill — registered in .config.json."""

    def __init__(self, gemini_key: str = "", muapi_key: str = "", evolink_key: str = ""):
        super().__init__()
        self._gemini_key = gemini_key
        self._muapi_key = muapi_key
        self._evolink_key = evolink_key

    @tool(
        "Запустить режим создания AI-короткометражки. "
        "Открывает веб-интерфейс для работы со сценарием."
    )
    async def launch(
        self,
        project_name: Annotated[str, "Имя проекта"] = "default",
    ) -> dict:
        movie = MovieTransport()
        movie.generator = Generator(movie, self._gemini_key, self._muapi_key, self._evolink_key)

        tab_skills = {
            "screenplay": ScreenplaySkill(movie),
            "characters":  CharactersSkill(movie),
            "storyboard": StoryboardSkill(movie),
        }
        control = _MovieControl()

        sub = await self.agent.spawn_subagent(
            f"movie_{project_name}",
            memory_providers=[],
            skills=[tab_skills["screenplay"], control],
            transport=MultiTransport([self.agent.transport, movie]),
        )
        movie.load(Path(sub.memory.memory_dir) / "project")

        def on_tab(tab: str):
            new_skill = tab_skills.get(tab)
            if not new_skill:
                return
            sub.skills = [s for s in sub.skills if s not in tab_skills.values()] + [new_skill]
            new_skill.register(sub)
        movie.on_tab_changed(on_tab)

        url = await movie.get_auth_url("/")
        await self.agent.transport.send_message(f"🎬 Movie Creator: {url}\nДля выхода: /exit")

        try:
            await stoppable(sub.loop(), control.done)
        finally:
            movie.cleanup()

        return {"status": "done", "project": movie.project.title}
