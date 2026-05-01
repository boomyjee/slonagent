"""TransportSkill — универсальный agent-side контракт «вывод от агента».

Tool'ы (send_images/send_files/send_suggestions) ходят в self.agent.transport,
который полиморфен — может быть конкретным транспортом или MultiTransport
с фан-аутом на несколько каналов. Транспорт реализует методы (`send_images`
и т.д.) на BaseTransport-уровне с каскадным дефолтом, конкретные транспорты
(Telegram, Web) переопределяют где есть нативная поддержка.
"""
import os
from typing import Annotated

from src.agent.skill import Skill, tool


class TransportSkill(Skill):
    def __init__(self):
        super().__init__()
        self.sandbox = None

    async def start(self):
        from src.skills.sandbox import SandboxSkill
        self.sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)

    def _resolve_paths(self, paths: list[str]) -> tuple[list[str] | None, dict | None]:
        host_paths = []
        for p in paths:
            # С sandbox'ом — резолвим container-path в host. Без — принимаем
            # host-абсолютный путь как есть.
            if self.sandbox:
                host_path = self.sandbox.resolve_path(p)
                if host_path is None:
                    return None, {"error": f"Доступ запрещён: {p}"}
            else:
                host_path = os.path.abspath(p)
            if not os.path.exists(host_path):
                return None, {"error": f"Файл не найден: {p}"}
            host_paths.append(host_path)
        return host_paths, None

    @tool(
        "Отправить пользователю одно или несколько изображений (альбом). "
        "Транспорт может пережать их под preview — если нужен оригинал без потерь, "
        "используй send_files."
    )
    async def send_images(
        self,
        paths: Annotated[list[str], "Список путей к изображениям."],
    ):
        host_paths, err = self._resolve_paths(paths)
        if err: return err
        await self.agent.transport.send_images(host_paths)
        return {"status": "ok"}

    @tool("Отправить пользователю один или несколько файлов как группу.")
    async def send_files(
        self,
        paths: Annotated[list[str], "Список путей к файлам."],
    ):
        host_paths, err = self._resolve_paths(paths)
        if err: return err
        await self.agent.transport.send_files(host_paths)
        return {"status": "ok"}

    @tool(
        "Предложить пользователю варианты ответа. "
        "Ответ юзера приходит как обычное сообщение. "
        "В транспортах с UI (Telegram) — отрисуется как кнопки; "
        "без UI — текстом нумерованным списком."
    )
    async def send_suggestions(
        self,
        text: Annotated[str, "Сообщение перед вариантами."],
        options: Annotated[list[str], "Варианты ответа."],
    ):
        await self.agent.transport.send_suggestions(text, options)
        return {"status": "ok"}
