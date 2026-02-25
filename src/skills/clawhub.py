from agent import Skill, tool
import os
import glob


class ClawhubSkill(Skill):
    def __init__(self):
        super().__init__()
        self._loaded_skills: dict[str, str] = {}  # name -> content

    def _skills_dir(self) -> str | None:
        from src.skills.sandbox import SandboxSkill
        sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None) if self.agent else None
        if sandbox is None:
            return None
        return os.path.join(sandbox.workspace_dir, "skills")

    def get_context_prompt(self, user_text: str = "") -> str:
        skills_dir = self._skills_dir()
        if not skills_dir:
            return ""

        self._loaded_skills = {}
        for path in glob.glob(os.path.join(skills_dir, "*", "SKILL.md")):
            name = os.path.basename(os.path.dirname(path))
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    self._loaded_skills[name] = content
            except Exception:
                pass

        if not self._loaded_skills:
            return ""

        parts = [
            "## Text Skills",
            "Управление скиллами через ClawHub:\n"
            "- поиск: `npx --yes clawhub@latest search \"...\" --limit 5`\n"
            "- установка: `npx --yes clawhub@latest install <slug> --workdir /workspace`\n"
            "- обновить все: `npx --yes clawhub@latest update --all --workdir /workspace`\n"
            "- список установленных: `npx --yes clawhub@latest list --workdir /workspace`",
        ]
        for name, content in self._loaded_skills.items():
            parts.append(f"### {name}\n{content}")
        return "\n\n".join(parts)

    @tool("Диагностика: показать список загруженных текстовых скиллов.")
    def list_text_skills(self) -> dict:
        return {
            "skills_dir": self._skills_dir(),
            "loaded": list(self._loaded_skills.keys()),
            "count": len(self._loaded_skills),
        }
