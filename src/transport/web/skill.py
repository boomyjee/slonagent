"""Скилы общие для любого WebTransport: ссылка на веб-интерфейс и скачивание
файлов, пришедших от пользователя через чат."""

import asyncio, logging, os, shutil
from typing import Annotated

from agent import Skill, bypass, tool

log = logging.getLogger(__name__)


class WebTransportSkill(Skill):
    """Скиллы общие для любого WebTransport: ссылка на веб-интерфейс и
    скачивание файлов, пришедших от пользователя через чат."""

    def __init__(self, transport):
        self.transport = transport
        self.sandbox = None
        super().__init__()

    async def start(self):
        from src.skills.sandbox import SandboxSkill
        self.sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)

    @bypass("web", "Ссылка на веб-интерфейс", standalone=True)
    async def web_command(self, args: str) -> str:
        return f"🖥 {await self.transport.fork.get_url('/')}"

    @tool(lambda self: (
        "Скачать файл, прикреплённый пользователем в чате. "
        "dest_path — путь внутри контейнера (напр. /workspace/file.jpg)."
        if self.sandbox
        else "Скачать файл, прикреплённый пользователем в чате. "
        "dest_path — абсолютный путь на хост-машине."
    ))
    async def download_file(
        self,
        file_id: Annotated[str, "id из метаданных attached_file."],
        dest_path: Annotated[str, "Путь назначения для сохранения файла."],
    ):
        uploads = self.transport.fork.uploads_dir
        # Файл сохранён как <uuid>.<ext> или просто <uuid> (если без расширения).
        matches = [e.path for e in os.scandir(uploads)
                   if e.is_file() and (e.name == file_id or e.name.startswith(file_id + "."))]
        if not matches:
            return {"error": f"Файл с id={file_id} не найден (возможно, удалён по TTL=24h)"}
        src = matches[0]

        if self.sandbox:
            host_dest = self.sandbox.resolve_path(dest_path)
            if host_dest is None:
                return {"error": f"Путь запрещён или недоступен: {dest_path}"}
        else:
            host_dest = os.path.abspath(dest_path)
        dest_dir = os.path.dirname(host_dest)
        if not os.path.isdir(dest_dir):
            return {"error": f"Директория не существует: {dest_dir}"}

        await asyncio.to_thread(shutil.copy, src, host_dest)
        log.info("[web] download_file: %s → %s", src, host_dest)
        return {"status": "ok", "saved_to": dest_path}
