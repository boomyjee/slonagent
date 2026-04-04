"""Movie Creator mode — AI-assisted short film production pipeline."""
import logging
import webbrowser
from pathlib import Path
from typing import Annotated

from agent import Skill, tool
from src.modes.movie_creator.project import Project
from src.modes.movie_creator.server import MovieServer
from src.modes.movie_creator.skills.characters import CharactersSkill
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
        characters_skill = CharactersSkill(project, server)
        tab_skills = {"screenplay": screenplay_skill, "characters": characters_skill}

        # Spawn subagent with default tab skill
        sub = await self.agent.spawn_subagent(
            f"movie_{project_name}",
            memory_providers=[],
            skills=[screenplay_skill],
        )

        def on_tab(tab):
            skill = tab_skills.get(tab)
            if skill:
                sub.skills = [skill]
                skill.register(sub)

        server.on_tab_changed(on_tab)

        # Web chat → show in Telegram + put in subagent queue
        async def _on_chat(text):
            await transport.inject_message(text)
            await transport.process_message([{"type": "text", "text": text}])

        server.on_chat(_on_chat)

        # Chat loop
        try:
            while True:
                content_parts, _ = await sub.next_message()
                msg = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict)).strip()
                await server.send_chat(msg, role="user")
                await sub.memory.add_turn({"role": "user", "content": content_parts})

                async def llm_with_processing():
                    await transport.send_processing(True)
                    await server.send_event("processing")
                    tc, text = await sub.llm()
                    await transport.send_processing(False)
                    await server.send_event("processing_done")
                    return tc, text

                tool_calls, reply = await llm_with_processing()

                if reply:
                    await server.send_chat(reply)

                while tool_calls:
                    for tc in tool_calls:
                        await server.send_event("tool_call", name=tc["function"]["name"])
                    await sub.dispatch_tool_calls(tool_calls)
                    tool_calls, reply = await llm_with_processing()
                    if reply:
                        await server.send_chat(reply)
        except Exception:
            log.exception("[movie_creator] error in chat loop")
            raise

        return {"status": "done", "project": project.title}
