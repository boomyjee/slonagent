import os, logging
from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaDocument
from aiogram.client.session.aiohttp import AiohttpSession
from google.genai import types


TELEGRAM_INSTRUCTIONS = (
    "Форматируй ответы в Telegram Markdown: "
    "*жирный*, _курсив_, `inline-код`, ```блок кода```. "
    "Команды, которые нужно скопировать, всегда оборачивай в `inline-код`. "
    "Не используй ** для жирного."
)


class TelegramOutputSkill:
    """
    Скилл, специфичный для Telegram.
    Отправляет файлы и изображения напрямую в чат во время tool call.
    """

    def __init__(self, path_resolver):
        self.path_resolver = path_resolver
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
        ]

    def set_message(self, message):
        self._message = message

    async def dispatch_tool_call(self, tool_call) -> dict:
        paths = tool_call.args.get("paths", [])
        host_paths = []
        for p in paths:
            host_path = self.path_resolver(p)
            if not os.path.exists(host_path):
                return {"error": f"Файл не найден: {host_path}"}
            host_paths.append(host_path)

        logging.info("[output] %s: %s", tool_call.name, host_paths)

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


class TelegramTransport:
    def __init__(self, bot_token: str, agent, path_resolver):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.bot = Bot(token=bot_token, session=AiohttpSession(proxy=proxy) if proxy else None)
        self.agent = agent
        self.dp = Dispatcher()
        self.dp.message()(self._handle_message)

        self._output_skill = TelegramOutputSkill(path_resolver=path_resolver)
        agent.skills.append(self._output_skill)

    async def _handle_message(self, message: Message):
        text = message.text or ""

        self._output_skill.set_message(message)
        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")

        try:
            async for part in self.agent.process_message(text=text, instructions=TELEGRAM_INSTRUCTIONS):
                if isinstance(part, str):
                    try:
                        await message.answer(part, parse_mode="Markdown")
                    except Exception:
                        await message.answer(part)
        except Exception as e:
            logging.exception("Error processing message")
            await message.answer(f"Произошла ошибка при обработке: {e}")

    async def start(self):
        logging.info("Starting TelegramTransport...")
        await self.dp.start_polling(self.bot)
