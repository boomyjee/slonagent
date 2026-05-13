import asyncio, json, os, sys
from src.agent.skill import Skill, bypass


class SubagentSkill(Skill):
    def __init__(self):
        super().__init__()
        self.exit_event = asyncio.Event()

    @bypass("exit", "Остановить текущий цикл", standalone=True)
    def exit_command(self, args: str) -> str:
        self.exit_event.set()
        return "⚠️ Цикл прерван пользователем."


class AgentSkill(Skill):
    """Встроенный скилл агента: базовые управляющие команды."""

    async def get_context_prompt(self, user_text: str = "") -> str:
        thread = self.agent.thread_id or "main"
        return f"Ты — агент `{self.agent.id}`, текущий тред — `{thread}`."

    @bypass("restart", "Перезапустить бота", standalone=True)
    async def restart_command(self, args: str) -> str:
        await self.agent.transport.send_message("🔄 Перезапускаюсь...")
        await asyncio.sleep(0.3)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    
    @bypass("stop", "Остановить текущий ответ", standalone=True)
    def stop_command(self, args: str) -> str:
        if self.agent:
            self.agent.stop()
        return "⚠️ Ответ прерван пользователем."

    @bypass("anonymous", "Переключить анонимный режим треда (не пишется в LOG.md)", standalone=True)
    def anonymous_command(self, args: str) -> str:
        threads = self.agent._load_threads()
        rec = threads.setdefault(self.agent.thread_id, {"name": ""})
        rec["anonymous"] = not rec.get("anonymous", False)
        self.agent._save_threads(threads)
        self.agent.memory.anonymous = rec["anonymous"]
        return f"🕶 Анонимный режим: {'включён' if rec['anonymous'] else 'выключен'}"
    
    @bypass("tool", "Вызвать тул")
    async def tool_command(self, args: str):
        parts = args.strip().split(maxsplit=1)
        if not parts:
            lines = []
            for skill in self.agent.skills:
                for fn in (d["function"] for d in skill.get_tools()):
                    params = fn.get("parameters", {}).get("properties", {})
                    line = f"`/tool {fn['name']}` — {fn.get('description', '')}"
                    for k, v in params.items():
                        req = "*" if k in fn.get("parameters", {}).get("required", []) else ""
                        line += f"\n  {k}{req}: {v.get('type', '?')}"
                    lines.append(line)
            return "Доступные инструменты:\n\n" + "\n\n".join(lines)
        
        name = parts[0]
        tool_to_skill = {decl["function"]["name"]: (skill, decl) for skill in self.agent.skills for decl in skill.get_tools()}
        entry = tool_to_skill.get(name)
        if not entry:  return f"Тул {name} не найден"
        skill, decl = entry
        arg_str = parts[1] if len(parts) > 1 else ""
        if arg_str.startswith("{"):
            try:
                parsed = json.loads(arg_str)
            except json.JSONDecodeError as e:
                return f"Невалидный JSON: {e}"
        else:
            param_names = list(decl["function"].get("parameters", {}).get("properties", {}).keys())
            parsed = {param_names[0]: arg_str} if arg_str and param_names else {}
        fc = {"id": f"bypass_{name}", "function": {"name": name, "arguments": json.dumps(parsed)}}

        async def run():
            await self.agent.transport.on_tool_call(name, parsed)
            await self.agent.transport.on_tool_result(name, await skill.dispatch_tool_call(fc))
        self.agent.call_before_next_message(run())
        return ""
