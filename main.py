import warnings

from agent import Agent
from src.transport.cli import CliTransport
from src.transport.telegram import TelegramTransport
from src.transport.multi import MultiTransport
from src.transport.dashboard import DashboardTransport

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

import asyncio, json, logging, os, sys
from src.skills.config import _format_json

with open(".config.json", encoding="utf-8") as f: config = json.load(f)
os.environ.update(config.get("env", {}))

def resolve(v):
    if isinstance(v, str) and v.startswith("$"):
        obj = config
        try:
            for part in v[1:].split("."):
                obj = obj[part]
        except (KeyError, TypeError):
            raise KeyError(f"Не найдена ссылка в конфиге: {v}")
        return obj
    if isinstance(v, dict):
        return {k: resolve(val) for k, val in v.items()}
    if isinstance(v, list):
        return [resolve(i) for i in v]
    return v

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
    agent = Agent.from_config(resolve(config["agent"]), id="main", agent_dir=os.getcwd(), transport=transport)
    await agent.start()

    print("CLI режим. Введите сообщение (Ctrl+C для выхода).")
    while True:
        text = await asyncio.get_event_loop().run_in_executor(None, input, "Вы: ")
        if text.strip():
            await transport.process_message(content_parts=[{"type": "text", "text": text}])
            print()

async def run_telegram():
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    DashboardTransport.set_server_config(**config.get("web", {}))

    async def make_agent(agent_id, transport, force_create: bool, copy_memory_from=None):
        is_main_agent = (agent_id == "main")
        agent_dir = os.getcwd() if is_main_agent else os.path.join(os.getcwd(), "forks", agent_id)
        if not force_create and not os.path.exists(agent_dir): return None

        config_path = os.path.join(agent_dir, ".config.json")
        if is_main_agent:
            agent_cfg = config["agent"]
        else:
            if not os.path.exists(config_path):
                os.makedirs(agent_dir, exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    merged = {**config["agent"], **resolve(config.get("fork_agent", {}))}
                    fork_config = {}
                    if "sandbox" in config:
                        fork_config["sandbox"] = config["sandbox"]
                    fork_config["agent"] = merged
                    f.write(_format_json(fork_config))
            with open(config_path, encoding="utf-8") as f:
                agent_cfg = json.load(f)["agent"]

        transport = MultiTransport([transport, DashboardTransport()])
        agent = Agent.from_config(resolve(agent_cfg), id=agent_id, agent_dir=agent_dir, transport=transport)
        if copy_memory_from:
            agent.memory.copy_from(copy_memory_from.agent.memory)
        await agent.start()
        return agent

    await TelegramTransport.listen(
        config=config["telegram"],
        make_agent=make_agent,
    )

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
