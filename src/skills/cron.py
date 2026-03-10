import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from agent import Skill, bypass, tool
from src.memory.memory import Memory

log = logging.getLogger(__name__)

REPEAT_UNITS = Literal["once", "hourly", "daily", "weekly"]


class CronSkill(Skill):
    def __init__(self):
        super().__init__()

    @property
    def _tasks_path(self) -> str:
        return os.path.join(Memory.memory_dir, "CRON.json")

    def _load_tasks(self) -> list[dict]:
        if not os.path.exists(self._tasks_path):
            return []
        try:
            with open(self._tasks_path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except Exception as e:
            log.warning("[cron] failed to load tasks: %s", e, exc_info=True)
            return []

    def _save_tasks(self, tasks: list[dict]) -> None:
        try:
            with open(self._tasks_path, "w", encoding="utf-8") as f:
                for task in tasks:
                    f.write(json.dumps(task, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("[cron] failed to save tasks: %s", e, exc_info=True)

    def _next_run(self, scheduled_at: str, repeat: str) -> str | None:
        dt = datetime.fromisoformat(scheduled_at)
        if repeat == "hourly":
            return (dt + timedelta(hours=1)).isoformat()
        if repeat == "daily":
            return (dt + timedelta(days=1)).isoformat()
        if repeat == "weekly":
            return (dt + timedelta(weeks=1)).isoformat()
        return None

    @tool("Запланировать задачу: агент проснётся в указанное время и выполнит заданное действие.")
    async def schedule_task(
        self,
        message: Annotated[str, "Что агент должен сделать или сообщить пользователю в указанное время."],
        scheduled_at: Annotated[str, "Время запуска в формате ISO 8601, например '2026-03-11T10:00:00'. Часовой пояс пользователя."],
        repeat: Annotated[REPEAT_UNITS, "Повторение: once — однократно, hourly — каждый час, daily — каждый день, weekly — каждую неделю."] = "once",
    ) -> dict:
        try:
            dt = datetime.fromisoformat(scheduled_at).astimezone()
        except ValueError:
            return {"error": f"Неверный формат даты: {scheduled_at!r}. Используй ISO 8601."}

        task = {
            "id": str(uuid.uuid4())[:8],
            "message": message,
            "scheduled_at": dt.isoformat(),
            "repeat": repeat,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        tasks = self._load_tasks()
        tasks.append(task)
        self._save_tasks(tasks)
        log.info("[cron] scheduled task %s at %s repeat=%s", task["id"], scheduled_at, repeat)
        return {"status": "scheduled", "task_id": task["id"], "scheduled_at": dt.strftime("%Y-%m-%dT%H:%M:%S"), "repeat": repeat}

    @tool("Отменить запланированную задачу по её ID.")
    async def cancel_task(
        self,
        task_id: Annotated[str, "ID задачи, которую нужно отменить."],
    ) -> dict:
        tasks = self._load_tasks()
        before = len(tasks)
        tasks = [t for t in tasks if t["id"] != task_id]
        if len(tasks) == before:
            return {"error": f"Задача {task_id!r} не найдена."}
        self._save_tasks(tasks)
        log.info("[cron] cancelled task %s", task_id)
        return {"status": "cancelled", "task_id": task_id}

    @tool("Показать список всех запланированных задач.")
    async def list_tasks(self) -> dict:
        tasks = self._load_tasks()
        if not tasks:
            return {"tasks": [], "message": "Нет запланированных задач."}
        return {"tasks": [
            {"id": t["id"], "scheduled_at": t["scheduled_at"], "repeat": t["repeat"], "message": t["message"]}
            for t in tasks
        ]}

    @bypass("cron", "Список запланированных задач", standalone=True)
    def cron_command(self, args: str) -> str:
        tasks = self._load_tasks()
        if not tasks:
            return "Нет запланированных задач."
        lines = ["Запланированные задачи:"]
        for t in tasks:
            repeat = f" [{t['repeat']}]" if t.get("repeat") != "once" else ""
            lines.append(f"  [{t['id']}] {t['scheduled_at']}{repeat} — {t['message']}")
        return "\n".join(lines)

    async def start(self):
        asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._tick()
            except Exception as e:
                log.warning("[cron] tick error: %s", e, exc_info=True)

    async def _tick(self):
        tasks = self._load_tasks()
        now = datetime.now().astimezone()
        remaining = []
        changed = False
        for task in tasks:
            try:
                due = datetime.fromisoformat(task["scheduled_at"]).astimezone()
            except ValueError:
                log.warning("[cron] invalid scheduled_at for task %s: %r", task["id"], task["scheduled_at"])
                changed = True
                continue

            if due <= now:
                log.info("[cron] firing task %s: %r", task["id"], task["message"])
                await self.agent.transport.inject_message(
                    f"⏰ Cron task [{task['id']}]: {task['message']}"
                )
                next_run = self._next_run(task["scheduled_at"], task.get("repeat", "once"))
                if next_run:
                    task["scheduled_at"] = next_run
                    remaining.append(task)
                changed = True
            else:
                remaining.append(task)

        if changed:
            self._save_tasks(remaining)
