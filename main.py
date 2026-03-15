import warnings

from aiogram import Bot, Dispatcher
from aiogram.types import Message, BotCommand
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

def resolve(v):
    if isinstance(v, str) and v.startswith("$"):
        key = v[1:]
        if key not in os.environ:
            raise KeyError(f"Переменная окружения ${key} не задана (проверь .config.json → env)")
        return os.environ[key]
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

with open(".config.json", encoding="utf-8") as f: config = json.load(f)
os.environ.update(config.get("env", {}))

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
    tg_cfg = config["telegram_transport"]

    dashboard = Dashboard(port=tg_cfg.get("dashboard_port", 8765))
    WrappedTG = dashboard.wrap(TelegramTransport)

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    bot = Bot(token=tg_cfg["bot_token"], session=AiohttpSession(proxy=proxy) if proxy else None)
    dp = Dispatcher()

    async def make_agent(chat_id: int, thread_id: int | None, agent_id: str, memory_dir: str = None):
        transport = WrappedTG(bot=bot, chat_id=chat_id, thread_id=thread_id, verbose=tg_cfg.get("verbose", True), agent_id=agent_id)
        agent = Agent(**resolve(config["agent"]), memory_dir=memory_dir, transport=transport)
        await agent.start()
        return agent

    allowed_user_ids = set(tg_cfg["allowed_user_ids"])
    main_chat_id = tg_cfg["allowed_user_ids"][0]
    main_agent = await make_agent(main_chat_id, None, "main")
    commands = [
        BotCommand(command=cmd, description=desc)
        for skill in main_agent.skills
        for cmd, desc in skill.get_bypass_commands(standalone_only=True).items()
    ]
    await bot.set_my_commands(commands)
    logging.info("[telegram] registered %d commands", len(commands))

    await dashboard.start()

    agents: dict[tuple, Agent] = {(main_chat_id, None): main_agent}

    async def on_message(message: Message):
        if not message.from_user or message.from_user.id not in allowed_user_ids:
            return

        thread_id = message.message_thread_id
        key = (message.chat.id, thread_id)

        if key not in agents:
            agent_id = f"thread_{message.chat.id}_{thread_id}"
            memory_dir = os.path.join(os.getcwd(), "memory", agent_id)
            agents[key] = await make_agent(message.chat.id, thread_id, agent_id, memory_dir)

        await agents[key].transport.handle_message(message)

    dp.message()(on_message)
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
