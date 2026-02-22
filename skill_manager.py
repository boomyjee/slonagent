import logging
import os
import sys
from typing import Annotated
from agent import Skill, tool


class SkillManager(Skill):
    def __init__(self, skills_dir: str = None):
        super().__init__()
        if skills_dir is None:
            root = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
            skills_dir = os.path.join(root, "memory", "skills")
        self._skills_dir = skills_dir
        os.makedirs(self._skills_dir, exist_ok=True)

        self._pending: dict[str, str] = {}
        self._active: dict[str, Skill] = {}
        self._saved_code: dict[str, str] = {}  # загружено с диска, ждёт register

        for fname in os.listdir(self._skills_dir):
            if not fname.endswith(".py"): continue
            name = fname[:-3]
            with open(os.path.join(self._skills_dir, fname), encoding="utf-8") as f:
                self._saved_code[name] = f.read()

    def register(self, agent):
        super().register(agent)
        for name, code in self._saved_code.items():
            result = self._instantiate(name, code)
            if "error" in result:
                logging.warning("[SkillManager] Не удалось загрузить %s: %s", name, result["error"])
        self._saved_code.clear()

    @tool("""Предложить новый инструмент для добавления в агент.
Код должен содержать класс-наследник Skill. Шаблон:

from agent import Skill, tool
from typing import Annotated

class MySkill(Skill):
    @tool("Описание того, что делает инструмент.")
    def my_tool(self, param: Annotated[str, "Описание параметра."]) -> dict:
        # реализация
        return {"result": ...}

Правила:
- класс наследует Skill
- каждый метод помечен @tool("описание")
- все параметры аннотированы через Annotated[тип, "описание"]
- метод возвращает dict
- можно импортировать стандартные библиотеки (os, json, re, httpx и др.)
- self.agent даёт доступ к агенту и другим скиллам через self.agent.skills

После вызова пользователь автоматически получит сообщение с кодом и инструкцией по активации.""")
    async def propose_skill(
        self,
        name: Annotated[str, "Имя скилла (snake_case)."],
        code: Annotated[str, "Полный Python-код скилла."],
    ):
        self._pending[name] = code
        if self.agent and self.agent.transport:
            await self.agent.transport.send_code("python", code)
            await self.agent.transport.send_message(f"Для активации: `/approve_skill {name}`\nДля удаления: `/delete_skill {name}`")
        return {"status": "pending"}

    _COMMANDS = {
        "approve_skill": lambda self, arg: self.approve_skill(arg),
        "delete_skill":  lambda self, arg: self.delete_skill(arg),
        "skills":        lambda self, arg: self.skills_list(),
    }

    def is_bypass_command(self, text: str) -> bool:
        return bool(text) and text.split()[0].lstrip("/") in self._COMMANDS

    def handle_bypass_command(self, text: str) -> str:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip("/")
        arg = parts[1].strip() if len(parts) > 1 else ""
        return self._COMMANDS[cmd](self, arg)

    def approve_skill(self, name: str) -> str:
        if name not in self._pending:
            return f"Нет pending-скилла {name!r}"
        code = self._pending.pop(name)
        result = self._instantiate(name, code)
        if "error" in result:
            self._pending[name] = code
            return f"Ошибка: {result['error']}"
        with open(os.path.join(self._skills_dir, name + ".py"), "w", encoding="utf-8") as f:
            f.write(code)
        return f"Скилл {name!r} активирован, тулы: {result['tools']}"

    def delete_skill(self, name: str) -> str:
        if name in self._pending:
            self._pending.pop(name)
            return f"Скилл {name!r} удалён"
        if name in self._active:
            self.agent.skills.remove(self._active.pop(name))
            os.remove(os.path.join(self._skills_dir, name + ".py"))
            return f"Скилл {name!r} удалён"
        return f"Скилл {name!r} не найден"

    def skills_list(self) -> str:
        rows = [f"pending  {n}" for n in sorted(self._pending)]
        rows += [f"active   {n}" for n in sorted(self._active)]
        return "\n".join(rows) if rows else "Скиллов нет"

    def _instantiate(self, name: str, code: str) -> dict:
        namespace = {"Skill": Skill, "tool": tool, "Annotated": Annotated}
        try:
            exec(code, namespace)
        except Exception as e:
            return {"error": str(e)}

        skill_class = next(
            (v for v in namespace.values() if isinstance(v, type) and issubclass(v, Skill) and v is not Skill),
            None,
        )
        if not skill_class:
            return {"error": "Не найден класс-наследник Skill"}

        instance = skill_class()
        instance.register(self.agent)
        self.agent.skills.append(instance)
        self._active[name] = instance
        return {"tools": [t.name for t in instance.tools]}
