import asyncio
import glob
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Annotated

from agent import Skill, bypass, tool


log = logging.getLogger(__name__)

_INTERVAL_RE = re.compile(r"^(\d+)\s*(s|m|h|d|w)$", re.IGNORECASE)
_INTERVAL_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_LEGACY_INTERVALS = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(weeks=1)}
_FILE_RE = re.compile(r"^CRON(?:_(.+))?\.json$")


class CronSkill(Skill):
    _root_dir: str | None = None
    _loop_task: asyncio.Task | None = None

    @classmethod
    def start_daemon(cls, root_dir: str | None = None):
        if cls._loop_task is not None:
            return
        cls._root_dir = root_dir or cls._root_dir or os.getcwd()
        cls._loop_task = asyncio.create_task(cls._loop())

    async def start(self):
        CronSkill.start_daemon()

    @classmethod
    async def _loop(cls):
        while True:
            await asyncio.sleep(30)
            try:
                await cls._tick()
            except Exception as e:
                log.warning("[cron] tick error: %s", e, exc_info=True)

    @classmethod
    async def _tick(cls):
        if cls._root_dir is None:
            return
        now = datetime.now().astimezone()
        for agent_id, thread_id, path in cls._discover_files(cls._root_dir):
            changed: dict[str, str | None] = {}  # id → новый scheduled_at | None (удалить)
            for task in cls._load_tasks(path):
                try:
                    due = datetime.fromisoformat(task["scheduled_at"]).astimezone()
                except ValueError:
                    log.warning("[cron] invalid scheduled_at for task %s: %r", task.get("id"), task.get("scheduled_at"))
                    changed[task["id"]] = None
                    continue
                if due > now:
                    continue
                agent = await cls._resolve_agent(agent_id, thread_id)
                if agent is None:
                    log.warning("[cron] task %s: no agent for %s/%s, retrying next tick",
                                task.get("id"), agent_id, thread_id)
                    continue
                log.info("[cron] firing task %s in %s/%s: %r",
                         task["id"], agent_id, thread_id, task["message"])
                text = f"⏰ Cron task [{task['id']}]: {task['message']}"
                try:
                    await agent.transport.inject_message(text)
                except Exception as e:
                    log.warning("[cron] inject failed for %s (continuing): %s", task.get("id"), e)
                try:
                    await agent.transport.process_message(
                        content_parts=[{"type": "text", "text": text}],
                        user_message_id=f"{task['id']}_{int(now.timestamp())}",
                    )
                except Exception as e:
                    log.warning("[cron] process failed for %s: %s", task.get("id"), e, exc_info=True)
                changed[task["id"]] = cls._next_run(task["scheduled_at"], task.get("repeat", "once"))
            if not changed:
                continue
            updated = []
            for t in cls._load_tasks(path):
                if t["id"] not in changed:
                    updated.append(t)
                elif (next_run := changed[t["id"]]) is not None:
                    updated.append({**t, "scheduled_at": next_run})
            cls._save_tasks(path, updated)

    @classmethod
    async def _resolve_agent(cls, agent_id: str, thread_id: str):
        from agent import Agent
        try:
            return await Agent.get(agent_id, thread_id)
        except Exception as e:
            log.warning("[cron] Agent.get(%s, %s) failed: %s", agent_id, thread_id, e, exc_info=True)
            return None

    # ─── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_interval(repeat: str) -> timedelta | None:
        if repeat in ("once", ""):
            return None
        if repeat in _LEGACY_INTERVALS:
            return _LEGACY_INTERVALS[repeat]
        m = _INTERVAL_RE.match(repeat.strip())
        if not m:
            raise ValueError(f"Неверный формат интервала: {repeat!r}. Используй: 30m, 2h, 1d, 7d и т.п.")
        return timedelta(seconds=int(m.group(1)) * _INTERVAL_SECONDS[m.group(2).lower()])

    @classmethod
    def _next_run(cls, scheduled_at: str, repeat: str) -> str | None:
        try:
            delta = cls._parse_interval(repeat)
        except ValueError:
            log.warning("[cron] invalid repeat %r, treating as once", repeat)
            delta = None
        if delta is None:
            return None
        next_dt = datetime.fromisoformat(scheduled_at) + delta
        now = datetime.now().astimezone()
        if next_dt <= now:
            skips = (now - next_dt) // delta + 1
            next_dt += skips * delta
        return next_dt.isoformat()

    @staticmethod
    def _load_tasks(path: str) -> list[dict]:
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except Exception as e:
            log.warning("[cron] failed to load tasks from %s: %s", path, e, exc_info=True)
            return []

    @staticmethod
    def _save_tasks(path: str, tasks: list[dict]) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                for task in tasks:
                    f.write(json.dumps(task, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("[cron] failed to save tasks to %s: %s", path, e, exc_info=True)

    @staticmethod
    def _discover_files(root_dir: str) -> list[tuple[str, str, str]]:
        """(agent_id, thread_id, path) для каждого CRON*.json в main и форках:
        `<root>/memory/CRON*.json` (agent_id="main") и
        `<root>/forks/<id>/memory/CRON*.json` (agent_id=<id>).
        thread_id берётся из имени файла: CRON.json → "", CRON_<tid>.json → <tid>."""
        out = []
        roots = [("main", os.path.join(root_dir, "memory"))]
        for fork_dir in glob.glob(os.path.join(root_dir, "forks", "*")):
            if os.path.isdir(fork_dir):
                roots.append((os.path.basename(fork_dir), os.path.join(fork_dir, "memory")))
        for agent_id, mem_dir in roots:
            for path in glob.glob(os.path.join(mem_dir, "CRON*.json")):
                m = _FILE_RE.match(os.path.basename(path))
                if m:
                    out.append((agent_id, m.group(1) or "", path))
        return out

    # ─── Instance side: tools ────────────────────────────────────────────────

    @property
    def _tasks_path(self) -> str:
        tid = self.agent.thread_id
        fname = f"CRON_{tid}.json" if tid else "CRON.json"
        return os.path.join(self.agent.memory.memory_dir, fname)

    @tool("Запланировать задачу: агент проснётся в указанное время и выполнит заданное действие.")
    async def schedule_task(
        self,
        message: Annotated[str, "Что агент должен сделать или сообщить пользователю в указанное время."],
        scheduled_at: Annotated[str, "Время запуска в формате ISO 8601, например '2026-03-11T10:00:00'. Часовой пояс пользователя."],
        repeat: Annotated[str, "Интервал повторения: 'once' — однократно, или произвольный интервал: '30m', '2h', '1d', '7d', '90s' и т.п. По умолчанию once."] = "once",
    ) -> dict:
        try:
            dt = datetime.fromisoformat(scheduled_at).astimezone()
        except ValueError:
            return {"error": f"Неверный формат даты: {scheduled_at!r}. Используй ISO 8601."}

        try:
            self._parse_interval(repeat)
        except ValueError as e:
            return {"error": str(e)}

        task = {
            "id": str(uuid.uuid4())[:8],
            "message": message,
            "scheduled_at": dt.isoformat(),
            "repeat": repeat,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        tasks = self._load_tasks(self._tasks_path)
        tasks.append(task)
        self._save_tasks(self._tasks_path, tasks)
        log.info("[cron] scheduled task %s at %s repeat=%s", task["id"], scheduled_at, repeat)
        return {"status": "scheduled", "task_id": task["id"], "scheduled_at": dt.strftime("%Y-%m-%dT%H:%M:%S"), "repeat": repeat}

    @tool("Отменить запланированную задачу по её ID.")
    async def cancel_task(
        self,
        task_id: Annotated[str, "ID задачи, которую нужно отменить."],
    ) -> dict:
        tasks = self._load_tasks(self._tasks_path)
        before = len(tasks)
        tasks = [t for t in tasks if t["id"] != task_id]
        if len(tasks) == before:
            return {"error": f"Задача {task_id!r} не найдена."}
        self._save_tasks(self._tasks_path, tasks)
        log.info("[cron] cancelled task %s", task_id)
        return {"status": "cancelled", "task_id": task_id}

    @tool("Показать список всех запланированных задач.")
    async def list_tasks(self) -> dict:
        tasks = self._load_tasks(self._tasks_path)
        if not tasks:
            return {"tasks": [], "message": "Нет запланированных задач."}
        return {"tasks": [
            {"id": t["id"], "scheduled_at": t["scheduled_at"], "repeat": t["repeat"], "message": t["message"]}
            for t in tasks
        ]}

    @bypass("cron", "Список запланированных задач", standalone=True)
    def cron_command(self, args: str) -> str:
        tasks = self._load_tasks(self._tasks_path)
        if not tasks:
            return "Нет запланированных задач."
        lines = ["Запланированные задачи:"]
        for t in tasks:
            repeat = f" [{t['repeat']}]" if t.get("repeat") != "once" else ""
            lines.append(f"  [{t['id']}] {t['scheduled_at']}{repeat} — {t['message']}")
        return "\n".join(lines)
