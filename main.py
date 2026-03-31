import warnings

from aiogram import Bot, Dispatcher
from aiogram.types import Message, BotCommand, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession

from agent import Agent
from src.transport.cli import CliTransport
from src.transport.telegram import TelegramTransport
from src.ui.dashboard import Dashboard

# Перехват warnings — единственный способ перекрыть то, что другие модули
# (в т.ч. зависимости) меняют формат логов и включают предупреждения внутри себя.
_original_warn = warnings.warn
def _warn(msg, category=UserWarning, stacklevel=1, source=None, **kw):
    if issubclass(category, (DeprecationWarning, ResourceWarning, FutureWarning)):
        return
    if isinstance(msg, (DeprecationWarning, ResourceWarning, FutureWarning)):
        return
    if getattr(category, "__module__", "").startswith("requests"):
        return
    return _original_warn(msg, category, stacklevel, source, **kw)
warnings.warn = _warn

import asyncio, importlib, json, logging, os, sys
from src.skills.config import _format_json

with open(".config.json", encoding="utf-8") as f: config = json.load(f)
os.environ.update(config.get("env", {}))

def resolve(v, instantiate=True):
    if isinstance(v, str) and v.startswith("$"):
        obj = config
        try:
            for part in v[1:].split("."):
                obj = obj[part]
        except (KeyError, TypeError):
            raise KeyError(f"Не найдена ссылка в конфиге: {v}")
        return obj
    if isinstance(v, dict):
        if instantiate and '__class__' in v: return _instantiate(v)
        return {k: resolve(val, instantiate) for k, val in v.items()}
    if isinstance(v, list):
        return [resolve(i, instantiate) for i in v]
    return v

def _instantiate(cfg: dict, cls=None):
    if cls is None:
        module_path, cls_name = cfg["__class__"].rsplit(".", 1)
        cls = getattr(importlib.import_module(module_path), cls_name)
    return cls(**{k: resolve(v) for k, v in cfg.items() if k != "__class__"})

_PID_PATH = ".agent.pid"

def acquire_pid_lock():
    import time, psutil
    try:
        pid = int(open(_PID_PATH).read().strip())
        if all(time.sleep(0.5) or psutil.pid_exists(pid) for _ in range(4)):
            logging.error("Агент уже запущен (PID %d). Завершаю.", pid)
            sys.exit(1)
    except Exception:
        pass
    open(_PID_PATH, "w").write(str(os.getpid()))

def release_pid_lock():
    try: os.unlink(_PID_PATH)
    except FileNotFoundError: pass

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

async def run_cli():
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    transport = CliTransport()
    agent = Agent(**resolve(config["agent"]), agent_dir=os.getcwd(), transport=transport)
    await agent.start()

    print("CLI режим. Введите сообщение (Ctrl+C для выхода).")
    while True:
        text = await asyncio.get_event_loop().run_in_executor(None, input, "Вы: ")
        if text.strip():
            await transport.process_message(content_parts=[{"type": "text", "text": text}])
            print()

async def run_telegram():
    dashboard = Dashboard(port=config.get("dashboard", {}).get("port", 8765))
    await dashboard.start()
    WrappedTG = dashboard.wrap(TelegramTransport)

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    bot = Bot(token=config["telegram"]["bot_token"], session=AiohttpSession(proxy=proxy) if proxy else None)
    dp = Dispatcher()

    allowed_user_ids = config["telegram"]["allowed_user_ids"]
    agents: dict[str, Agent] = {}

    async def make_agent(chat_id: int, thread_id: int, force_create: bool, copy_memory_from=None):
        is_main_agent = (chat_id == allowed_user_ids[0] and thread_id is None)
        agent_id = "main" if is_main_agent else f"{chat_id}_{thread_id}"
        if agent_id in agents: return agents[agent_id]

        agent_dir = os.getcwd() if is_main_agent else os.path.join(os.getcwd(), "forks", agent_id)
        if not force_create and not os.path.exists(agent_dir): return None

        config_path = os.path.join(agent_dir, ".config.json")
        if is_main_agent:
            agent_cfg = config["agent"]
        else:
            if not os.path.exists(config_path):
                os.makedirs(agent_dir, exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    merged = {**config["agent"], **resolve(config.get("fork_agent", {}), instantiate=False)}
                    fork_config = {}
                    if "sandbox" in config:
                        fork_config["sandbox"] = config["sandbox"]
                    fork_config["agent"] = merged
                    f.write(_format_json(fork_config))
            with open(config_path, encoding="utf-8") as f:
                agent_cfg = json.load(f)["agent"]

        transport = WrappedTG(**config["telegram"]["transport"], bot=bot, chat_id=chat_id, thread_id=thread_id, agent_id=agent_id)
        agent = Agent(**resolve(agent_cfg), agent_dir=agent_dir, transport=transport)
        if copy_memory_from:
            agent.memory.copy_from(copy_memory_from.memory)
        await agent.start()
        agents[agent_id] = agent
        return agent

    main_agent = await make_agent(allowed_user_ids[0], None, force_create=True)

    commands = [
        BotCommand(command=cmd, description=desc)
        for skill in main_agent.skills
        for cmd, desc in skill.get_bypass_commands(standalone_only=True).items()
    ]
    await bot.set_my_commands(commands)
    logging.info("[telegram] registered %d commands", len(commands))

    async def on_message(message: Message):
        if not message.from_user or message.from_user.id not in allowed_user_ids:
            return

        agent = await make_agent(message.chat.id, message.message_thread_id, force_create=False)
        if not agent:
            if message.text:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🗒 Чистый агент", callback_data="new_agent_clean"),
                    InlineKeyboardButton(text="🧠 Клон с памятью", callback_data="new_agent_clone"),
                ]])
                await message.answer(
                    "В этом топике ещё нет агента.\n\n"
                    "• <b>🗒 Чистый агент</b> — начнёт с нуля, без памяти\n"
                    "• <b>🧠 Клон с памятью</b> — скопирует личность и инструменты из основного агента",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            return

        await agent.transport.handle_message(message)

    async def on_callback_query(callback: CallbackQuery):
        if not callback.from_user or callback.from_user.id not in allowed_user_ids: return

        chat_id = callback.message.chat.id
        thread_id = callback.message.message_thread_id

        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)

        if callback.data.startswith("answer:"):
            text = callback.data[len("answer:"):]
            agent = await make_agent(chat_id, thread_id, force_create=False)
            if agent:
                await callback.message.answer(text)
                await agent.transport.process_message(content_parts=[{"type": "text", "text": text}])
            return

        if callback.data in ("new_agent_clean", "new_agent_clone"):
            status = await callback.message.answer("⏳ Создаю агента...")
            copy_from = main_agent if callback.data == "new_agent_clone" else None
            await make_agent(chat_id, thread_id, force_create=True, copy_memory_from=copy_from)

            label = "✅ Клон создан" if callback.data == "new_agent_clone" else "✅ Агент создан"
            await status.edit_text(label)

    dp.message()(on_message)
    dp.callback_query()(on_callback_query)
    await dp.start_polling(bot)

acquire_pid_lock()
try:
    if "--cli" in sys.argv:
        asyncio.run(run_cli())
    else:
        asyncio.run(run_telegram())
except (KeyboardInterrupt, asyncio.CancelledError):
    pass
finally:
    release_pid_lock()
