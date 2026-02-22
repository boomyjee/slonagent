import io, os, asyncio, logging, json, mimetypes
from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaDocument, LinkPreviewOptions
from aiogram.client.session.aiohttp import AiohttpSession
from google.genai import types


TELEGRAM_INSTRUCTIONS = (
    "Форматируй ответы в Telegram Markdown: "
    "*жирный*, _курсив_, `inline-код`, ```блок кода```. "
    "Команды, которые нужно скопировать, всегда оборачивай в `inline-код`. "
    "Не используй ** для жирного."
)

class TelegramSkill:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.agent = None
        self._message = None
        self.tools = [
            types.FunctionDeclaration(
                name="send_files",
                description="Отправить один или несколько файлов как группу. Один вызов = одна группа. Для нескольких групп — вызови несколько раз.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "paths": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description="Список путей к файлам внутри контейнера.",
                        ),
                    },
                    required=["paths"],
                ),
            ),
            types.FunctionDeclaration(
                name="send_images",
                description="Отправить одно или несколько изображений как альбом (до 10). Один вызов = один альбом. Для нескольких альбомов — вызови несколько раз.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "paths": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description="Список путей к изображениям внутри контейнера.",
                        ),
                    },
                    required=["paths"],
                ),
            ),
            types.FunctionDeclaration(
                name="download_file",
                description=(
                    "Скачать файл, отправленный пользователем, в рабочую директорию. "
                    "Путь назначения должен быть внутри /workspace/."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "tg_file_id": types.Schema(
                            type=types.Type.STRING,
                            description="tg_file_id из метаданных прикреплённого файла.",
                        ),
                        "dest_path": types.Schema(
                            type=types.Type.STRING,
                            description="Путь назначения внутри контейнера (например /workspace/photo.jpg).",
                        ),
                    },
                    required=["tg_file_id", "dest_path"],
                ),
            ),
        ]

    def set_message(self, message):
        self._message = message

    async def dispatch_tool_call(self, tool_call) -> dict:
        if tool_call.name == "download_file":
            return await self._download_file(
                tg_file_id=tool_call.args.get("tg_file_id"),
                dest_path=tool_call.args.get("dest_path"),
            )

        from exec import ExecSkill
        exec_skill = next((s for s in self.agent.skills if isinstance(s, ExecSkill)), None)
        paths = tool_call.args.get("paths", [])
        host_paths = []
        for p in paths:
            host_path = exec_skill.resolve_path(p) if exec_skill else None
            if host_path is None:
                return {"error": f"Доступ запрещён: {p}"}
            if not os.path.exists(host_path):
                return {"error": f"Файл не найден: {host_path}"}
            host_paths.append(host_path)

        logging.info("[skill] %s: %s", tool_call.name, host_paths)

        if tool_call.name == "send_images":
            if len(host_paths) == 1:
                await self._message.answer_photo(FSInputFile(host_paths[0]))
            else:
                media = [InputMediaPhoto(media=FSInputFile(p)) for p in host_paths]
                await self._message.answer_media_group(media)
        else:
            if len(host_paths) == 1:
                await self._message.answer_document(FSInputFile(host_paths[0]))
            else:
                media = [InputMediaDocument(media=FSInputFile(p)) for p in host_paths]
                await self._message.answer_media_group(media)

        return {"status": "ok"}

    async def _download_file(self, tg_file_id: str, dest_path: str) -> dict:
        from exec import ExecSkill
        exec_skill = next((s for s in self.agent.skills if isinstance(s, ExecSkill)), None)
        host_dest = exec_skill.resolve_path(dest_path) if exec_skill else None

        if host_dest is None:
            return {"error": f"Путь запрещён или недоступен: {dest_path}"}

        tg_file = await self.bot.get_file(tg_file_id)
        os.makedirs(os.path.dirname(host_dest), exist_ok=True)
        await self.bot.download_file(tg_file.file_path, host_dest)
        logging.info("[skill] download_file: %s → %s", tg_file_id, host_dest)
        return {"status": "ok", "saved_to": dest_path}


class TelegramTransport:
    def __init__(self, bot_token: str, agent):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.bot = Bot(token=bot_token, session=AiohttpSession(proxy=proxy) if proxy else None)
        self.agent = agent
        self.dp = Dispatcher()
        self.dp.message()(self._handle_message)

        self._skill = TelegramSkill(self.bot)
        self._skill.agent = agent
        agent.skills.append(self._skill)

        self._media_groups: dict[str, list[Message]] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}

    async def on_tool_call(self, name: str, args: dict):
        if not args:
            self._tool_call_text = f"<b>[{name}]</b>"
        elif len(args) == 1:
            self._tool_call_text = f"<b>[{name}]</b> {next(iter(args.values()))}"
        else:
            lines = "\n".join(f"  {k}: {v}" for k, v in args.items())
            self._tool_call_text = f"<b>[{name}]</b>\n{lines}"
        self._tool_msg = await self._current_message.answer(
            f"<blockquote expandable>{self._tool_call_text}</blockquote>",
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def on_tool_result(self, name: str, result):
        if isinstance(result, dict):
            parts = [f"<b>[{k}]</b>\n{v}" for k, v in result.items() if v not in (None, "", [], {})]
            result_text = "\n".join(parts) if parts else "(пусто)"
        else:
            result_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        try:
            await self._tool_msg.edit_text(
                f"<blockquote expandable>{self._tool_call_text}</blockquote>\n<blockquote expandable>{result_text}</blockquote>",
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            pass

    async def on_content(self, text: str):
        try:
            await self._current_message.answer(text, parse_mode="Markdown")
        except Exception:
            await self._current_message.answer(text)

    async def _handle_message(self, message: Message):
        if message.media_group_id:
            group_id = message.media_group_id
            self._media_groups.setdefault(group_id, []).append(message)
            if group_id in self._media_group_tasks:
                self._media_group_tasks[group_id].cancel()
            self._media_group_tasks[group_id] = asyncio.create_task(
                self._flush_media_group(group_id)
            )
        else:
            await self._process_messages([message])

    async def _flush_media_group(self, group_id: str):
        await asyncio.sleep(0.1)
        messages = self._media_groups.pop(group_id, [])
        self._media_group_tasks.pop(group_id, None)
        await self._process_messages(messages)

    async def _process_messages(self, messages: list[Message]):
        first = messages[0]
        self._current_message = first
        self._tool_msg = None
        self._tool_call_text = ""

        self._skill.set_message(first)
        await self.bot.send_chat_action(chat_id=first.chat.id, action="typing")

        message_parts = []

        for message in messages:
            text = message.text or message.caption
            if text: message_parts.append({"text":text})

            if message.photo:
                tg_file = await self.bot.get_file(message.photo[-1].file_id)
                buf = io.BytesIO()
                await self.bot.download_file(tg_file.file_path, buf)
                message_parts.append(
                    types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")
                )

            attachment, field = None, None
            if message.photo: attachment, field = message.photo[-1], "photo"
            elif message.audio: attachment, field = message.audio, "audio"
            elif message.video: attachment, field = message.video, "video"
            elif message.voice: attachment, field = message.voice, "voice"
            elif message.document: attachment, field = message.document, "document"
            else: continue

            filename = f"photo_{attachment.file_unique_id}.jpg" if field == "photo" else getattr(attachment, "file_name", None)
            file_meta = {
                "tg_file_id": attachment.file_id,
                "filename": filename,
                "type": field,
                "mime_type": mimetypes.guess_type(filename or "")[0] or "application/octet-stream",
                "size_bytes": attachment.file_size,
            }

            text_extensions = {
                ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".xml",
                ".html", ".css", ".sh", ".sql", ".csv", ".log",
            }
            if field == "document" and os.path.splitext(filename or "")[1].lower() in text_extensions:
                try:
                    tg_file = await self.bot.get_file(attachment.file_id)
                    buf = io.BytesIO()
                    await self.bot.download_file(tg_file.file_path, buf)
                    file_meta["content"] = buf.getvalue().decode("utf-8", errors="replace")
                except Exception:
                    logging.exception("[transport] Не удалось прочитать текстовый файл %s", filename)

            message_parts.append({"text": json.dumps(file_meta, ensure_ascii=False)})


        try:
            await self.agent.process_message(
                message_parts=message_parts,
                instructions=TELEGRAM_INSTRUCTIONS,
                transport=self,
            )
        except Exception as e:
            logging.exception("Error processing message")
            await first.answer(f"Произошла ошибка при обработке: {e}")

    async def start(self):
        logging.info("Starting TelegramTransport...")
        await self.dp.start_polling(self.bot)
