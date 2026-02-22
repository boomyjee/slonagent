import os, logging
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession


class TelegramTransport:
    def __init__(self, bot_token: str, agent):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.bot = Bot(token=bot_token, session=AiohttpSession(proxy=proxy) if proxy else None)
        self.agent = agent
        self.dp = Dispatcher()
        self.dp.message()(self._handle_message)

    async def _handle_message(self, message: Message):
        text = message.text

        await self.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        try:
            response_text = await self.agent.process_message(text=text)
            await message.answer(response_text)
        except Exception as e:
            logging.exception("Error processing message")
            await message.answer(f"Произошла ошибка при обработке: {e}")

    async def start(self):
        logging.info("Starting TelegramTransport...")
        await self.dp.start_polling(self.bot)
