"""Movie Creator mode — AI-assisted short film production pipeline."""
import logging
from pathlib import Path
from typing import Annotated

from agent import Skill, tool
from src.modes.movie_creator.generator import Generator
from src.modes.movie_creator.server import MovieServer
from src.modes.movie_creator.skills.characters import CharactersSkill
from src.modes.movie_creator.skills.screenplay import ScreenplaySkill
from src.modes.movie_creator.skills.storyboard import StoryboardSkill
from src.modes.movie_creator.transport import WebTransport
from src.transport.multi import MultiTransport

log = logging.getLogger(__name__)


class MovieCreatorSkill(Skill):
    """Entry point skill — registered in .config.json."""

    def __init__(self, port: int = 3210, gemini_key: str = "", muapi_key: str = "", evolink_key: str = ""):
        super().__init__()
        self._port = port
        self._gemini_key = gemini_key
        self._muapi_key = muapi_key
        self._evolink_key = evolink_key

    @tool(
        "Запустить режим создания AI-короткометражки. "
        "Открывает веб-интерфейс для работы со сценарием."
    )
    async def start_movie_creator(
        self,
        project_name: Annotated[str, "Имя проекта"] = "default",
    ) -> dict:

        sub = await self.agent.spawn_subagent(
            f"movie_{project_name}",
            memory_providers=[],
            skills=[],
        )

        project_dir = Path(sub.memory.memory_dir) / "project"
        server = MovieServer(self._port, project_dir)
        server.generator = Generator(server, self._gemini_key, self._muapi_key, self._evolink_key)
        await server.start()
        await self.agent.transport.send_message(f"Movie Creator: {server.url}")

        multi = MultiTransport([self.agent.transport, WebTransport(server)])
        sub.transport = multi
        multi.set_agent(sub)

        tab_skills = {
            "screenplay": ScreenplaySkill(server),
            "characters": CharactersSkill(server),
            "storyboard": StoryboardSkill(server),
        }

        def on_tab(tab):
            skill = tab_skills.get(tab)
            if skill:
                sub.skills = [skill]
                skill.register(sub)
        server.on_tab_changed(on_tab)
        on_tab("screenplay")

        # Chat loop
        while True:
            content_parts, _ = await sub.next_message()
            await sub.transport.send_processing(True)
            try:
                await sub.memory.add_turn({"role": "user", "content": content_parts})
                turn = await sub.llm()

                while turn.get("tool_calls"):
                    result_turns = await sub.dispatch_tool_calls(turn)
                    await sub.memory.add_turn(turn, *result_turns)
                    turn = await sub.llm()

                await sub.memory.add_turn(turn)
            except Exception as e:
                log.exception("[movie_creator] error in chat loop")
                await sub.transport.send_message(f"Ошибка: {e}")
            finally:
                await sub.transport.send_processing(False)

        return {"status": "done", "project": server.project.title}
