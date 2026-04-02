"""Run diary enrichment as a standalone app with its own Telegram bot."""
import asyncio
import json
import logging
import os

os.chdir(os.path.join(os.path.dirname(__file__), "..", ".."))

with open("scripts/diary_enrichment/config.json", encoding="utf-8") as f:
    config = json.load(f)
os.environ.update(config.get("env", {}))

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession

import tempfile

from agent import Agent
from src.transport.telegram import TelegramTransport

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


async def main():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    bot = Bot(token=config["telegram"]["bot_token"], session=AiohttpSession(proxy=proxy) if proxy else None)
    dp = Dispatcher()

    allowed = config["telegram"]["allowed_user_ids"]
    chat_id = allowed[0]

    transport = TelegramTransport(bot=bot, chat_id=chat_id, thread_id=None, agent_id="diary", verbose=False)
    agent = Agent.from_config(config["agent"],
        agent_dir=tempfile.mkdtemp(),
        transport=transport,
    )
    await agent.start(run_loop=False)

    skill = next(s for s in agent.skills if hasattr(s, 'start_enrichment'))

    async def run_enrichment():
        await skill.start_enrichment()
        dp.stop_polling()

    async def on_message(message: Message):
        if not message.from_user or message.from_user.id not in allowed: return
        await agent.transport.handle_message(message)

    dp.message()(on_message)
    await bot.get_updates(offset=-1)
    asyncio.create_task(run_enrichment())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
