import warnings

from agent import Agent
from src.transport.cli import CliTransport
from src.transport.multi import MultiTransport
from src.transport.telegram import TelegramTransport
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

import asyncio, logging, logging.handlers, os, sys
from src.skills.cron import CronSkill

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

async def run_cli():
    DashboardTransport.start(Agent.config.get("web", {}))
    CronSkill.start_daemon(root_dir=os.getcwd())
    Agent.transport_factory = lambda: MultiTransport([
        CliTransport(),
        DashboardTransport(**Agent.config.get("web", {}).get("transport", {})),
    ])
    agent = await Agent.get("main")

    print("CLI режим. Введите сообщение (Ctrl+C для выхода).")
    while True:
        text = await asyncio.get_event_loop().run_in_executor(None, input, "Вы: ")
        if text.strip():
            await agent.transport.process_message(content_parts=[{"type": "text", "text": text}])
            print()

async def run_telegram():
    DashboardTransport.start(Agent.config.get("web", {}))
    TelegramTransport.start(Agent.config.get("telegram", {}))
    CronSkill.start_daemon(root_dir=os.getcwd())
    Agent.transport_factory = lambda: MultiTransport([
        DashboardTransport(**Agent.config.get("web", {}).get("transport", {})),
        TelegramTransport(**Agent.config.get("telegram", {}).get("transport", {})),
    ])
    await Agent.get("main")
    await asyncio.Event().wait()

acquire_pid_lock()
os.makedirs(_LOG_DIR := os.path.expanduser("~/.slonagent"), exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "slonagent.log")
_log_fh = logging.handlers.RotatingFileHandler(_LOG_FILE, maxBytes=0, backupCount=3, encoding="utf-8")
if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > 0: _log_fh.doRollover()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", handlers=[
    logging.StreamHandler(), _log_fh,
])

try:
    if "--cli" in sys.argv:
        asyncio.run(run_cli())
    else:
        asyncio.run(run_telegram())
except (KeyboardInterrupt, asyncio.CancelledError):
    pass
finally:
    release_pid_lock()
