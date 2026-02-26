import logging, os
from typing import Annotated
from agent import tool
from src.memory.base import BaseProvider
from memory import Memory, load_json, save_json


class KnowledgeProvider(BaseProvider):
    def __init__(self):
        super().__init__(consolidate_tokens=0)
        self._knowledge_dir = os.path.join(Memory.memory_dir, "knowledge")
        os.makedirs(self._knowledge_dir, exist_ok=True)
        self._active_file = os.path.join(self._knowledge_dir, ".active.json")
        self._active: list[str] = load_json(self._active_file, [])

    def _module_path(self, name: str) -> str:
        return os.path.join(self._knowledge_dir, f"{name}.md")

    def _read_module(self, name: str) -> tuple[str, str]:
        path = self._module_path(name)
        if not os.path.exists(path):
            return "", ""
        with open(path, encoding="utf-8") as f:
            text = f.read()
        parts = text.split("---", 1)
        description = parts[0].strip()
        content = parts[1].strip() if len(parts) > 1 else ""
        return description, content

    def _write_module(self, name: str, description: str, content: str):
        with open(self._module_path(name), "w", encoding="utf-8") as f:
            f.write(f"{description}\n---\n{content}")

    def _list_modules(self) -> list[str]:
        return sorted(
            f[:-3] for f in os.listdir(self._knowledge_dir)
            if f.endswith(".md")
        )

    def get_context_prompt(self, user_text: str = "") -> str:
        modules = self._list_modules()
        active_valid = [n for n in self._active if n in modules]

        if not modules:
            modules_block = "(баз знаний пока нет)"
        else:
            lines = []
            for name in modules:
                description, content = self._read_module(name)
                if name in active_valid:
                    lines.append(f"- **{name}** — {description} [загружена]")
                else:
                    lines.append(f"- **{name}** — {description} ({len(content)} симв.)")
            modules_block = "\n".join(lines)

        instruction = (
            "Базы знаний — модули с накопленной информацией, которые ты сам ведёшь.\n"
            "Загруженные базы попадают в контекст целиком.\n"
            "load_knowledge принимает полный список нужных баз — те, что не перечислены, автоматически выгружаются.\n"
            "Обновлять (update_knowledge) можно только загруженные базы.\n"
            "Если в разговоре появляется информация, полезная для загруженной базы — обнови её сразу.\n"
            "Если нужной базы нет — создай новую через create_knowledge."
        )

        parts = [f"## Базы знаний\n{modules_block}\n\n{instruction}"]

        for name in active_valid:
            description, content = self._read_module(name)
            parts.append(f"### [{name}] {description}\n{content}")

        return "\n\n".join(parts)

    @tool("Установить набор активных баз знаний. Перечисленные загружаются в контекст, остальные автоматически выгружаются.")
    def load_knowledge(self, names: Annotated[list[str], "Список имён баз знаний для активации"]) -> dict:
        available = self._list_modules()
        unknown = [n for n in names if n not in available]
        if unknown:
            return {"error": f"Не найдены: {', '.join(unknown)}. Доступные: {', '.join(available)}"}
        self._active = list(names)
        save_json(self._active_file, self._active)
        logging.info("[KnowledgeProvider] активные базы: %s", self._active)
        return {"loaded": self._active}

    @tool("Обновить содержимое загруженной базы знаний.")
    def update_knowledge(
        self,
        name: Annotated[str, "Имя базы знаний"],
        content: Annotated[str, "Полное новое содержимое базы"],
    ) -> dict:
        if name not in self._active:
            return {"error": f"База '{name}' не загружена. Загруженные: {self._active}. Сначала вызови load_knowledge."}
        if not os.path.exists(self._module_path(name)):
            return {"error": f"База '{name}' не найдена."}
        description, _ = self._read_module(name)
        self._write_module(name, description, content)
        logging.info("[KnowledgeProvider] обновлена база: %s", name)
        return {"updated": name}

    @tool("Создать новую базу знаний.")
    def create_knowledge(
        self,
        name: Annotated[str, "Имя базы (латиница, snake_case, без пробелов)"],
        description: Annotated[str, "Короткое описание (1 строка)"],
        content: Annotated[str, "Начальное содержимое"],
    ) -> dict:
        if os.path.exists(self._module_path(name)):
            return {"error": f"База '{name}' уже существует. Для обновления используй update_knowledge."}
        self._write_module(name, description, content)
        logging.info("[KnowledgeProvider] создана база: %s", name)
        return {"created": name}
