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

with open(".config.json", encoding="utf-8") as f: config = json.load(f)
os.environ.update(config.get("env", {}))

def resolve(v):
    if isinstance(v, str) and v.startswith("$"):
        obj = config
        path = v[1:].split(".")
        try:
            for part in path:
                obj = obj[part]
        except (KeyError, TypeError):
            raise KeyError(f"Не найдена ссылка в конфиге: {v}")
        return resolve(obj)
    if isinstance(v, dict):
        if '__class__' in v: return instantiate(v)
        return {k: resolve(val) for k, val in v.items()}
    if isinstance(v, list):
        return [resolve(i) for i in v]
    return v

def instantiate(cfg: dict, cls=None):
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
    agent = Agent(**resolve(config["agent"]), transport=transport)
    await agent.start()

    print("CLI режим. Введите сообщение (Ctrl+C для выхода).")
    while True:
        text = await asyncio.get_event_loop().run_in_executor(None, input, "Вы: ")
        if text.strip():
            await agent.process_message(message_parts=[{"text": text}])
            print()

async def run_telegram():
    dashboard = Dashboard(port=config.get("dashboard", {}).get("port", 8765))
    await dashboard.start()
    WrappedTG = dashboard.wrap(TelegramTransport)

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    bot = Bot(token=config["telegram"]["bot_token"], session=AiohttpSession(proxy=proxy) if proxy else None)
    dp = Dispatcher()

    agents: dict[str, Agent] = {}

    async def make_agent(chat_id: int, thread_id: int, force_create: bool, is_main_agent: bool = False):
        agent_id = f"{chat_id}_{thread_id}"
        if agent_id in agents: return agents[agent_id]

        agent_dir = os.getcwd() if is_main_agent else os.path.join(os.getcwd(), "forks", agent_id)
        memory_dir = os.path.join(agent_dir, "memory")
        if not force_create and not os.path.exists(agent_dir): return None

        agent_cfg = config["agent"] if is_main_agent else {**config["agent"], **config.get("fork_agent", {})}
        transport = WrappedTG(**config["telegram"]["transport"], bot=bot, chat_id=chat_id, thread_id=thread_id, agent_id=agent_id)
        agent = Agent(**resolve(agent_cfg), memory_dir=memory_dir, transport=transport)
        await agent.start()
        agents[agent_id] = agent
        return agent

    allowed_user_ids = config["telegram"]["allowed_user_ids"]
    main_agent = await make_agent(allowed_user_ids[0], None, force_create=True, is_main_agent=True)

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
                    InlineKeyboardButton(text="Создать агента", callback_data="new_agent")
                ]])
                await message.reply("Агента для этого топика нет. Создать?", reply_markup=kb)
            return

        await agent.transport.handle_message(message)

    async def on_callback_query(callback: CallbackQuery):
        if callback.data != "new_agent": return
        if not callback.from_user or callback.from_user.id not in allowed_user_ids: return
        await make_agent(callback.message.chat.id, callback.message.message_thread_id, force_create=True)
        await callback.answer("Агент создан")
        await callback.message.edit_text("Агент создан ✓")

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
