"""Movie Creator mode — AI-assisted short film production pipeline."""
import asyncio
import logging
import webbrowser
from pathlib import Path
from typing import Annotated

from agent import Skill, tool
from src.modes.movie_creator.project import Project
from src.modes.movie_creator.server import MovieServer
from src.modes.movie_creator.skills.screenplay import ScreenplaySkill

log = logging.getLogger(__name__)


class MovieCreatorSkill(Skill):
    """Entry point skill — registered in .config.json."""

    def __init__(self, port: int = 3210):
        super().__init__()
        self._port = port

    @tool(
        "Запустить режим создания AI-короткометражки. "
        "Открывает веб-интерфейс для работы со сценарием."
    )
    async def start_movie_creator(
        self,
        project_name: Annotated[str, "Имя проекта"] = "default",
    ) -> dict:
        transport = self.agent.transport
        movies_dir = Path(self.agent.memory.memory_dir) / "movies"
        project_dir = movies_dir / project_name

        if (project_dir / "project.json").exists():
            project = Project.load(project_dir)
        else:
            project = Project(project_dir, title=project_name)
            project.save()

        server = MovieServer(self._port, project)
        await server.start()

        url = f"http://localhost:{self._port}"
        await transport.send_message(f"Movie Creator: {url}")
        webbrowser.open(url)

        # Tab skills
        screenplay_skill = ScreenplaySkill(project, server)
        tab_skills = {"screenplay": screenplay_skill}
        active_skill = screenplay_skill

        def on_tab(tab):
            nonlocal active_skill
            active_skill = tab_skills.get(tab)

        server.on_tab_changed(on_tab)

        # Spawn subagent
        sub = await self.agent.spawn_subagent(
            f"movie_{project_name}",
            memory_providers=[],
            skills=[screenplay_skill],
        )

        # Chat loop
        try:
            while True:
                text = await server.wait_for_chat()
                await sub.memory.add_turn(
                    {"role": "user",
                     "content": [{"type": "text", "text": text}]})

                tool_calls, reply = await sub.llm()

                if reply:
                    await server.send_chat(reply)

                while tool_calls:
                    await sub.dispatch_tool_calls(tool_calls)
                    tool_calls, reply = await sub.llm()
                    if reply:
                        await server.send_chat(reply)
        except Exception:
            log.exception("[movie_creator] error in chat loop")
            raise

        return {"status": "done", "project": project.title}
