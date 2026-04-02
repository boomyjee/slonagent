from typing import Annotated

from agent import Skill, tool


class AnecdoteLoopSkill(Skill):

    @tool("Запустить цикл анекдотов: ЛЛМ рассказывает анекдот, пользователь ставит оценку от 1 до 10, ЛЛМ учитывает её в следующем. Завершается когда пользователь пишет 'хватит'.")
    async def start_anecdote_loop(self) -> dict:
        class TellJokeSkill(Skill):
            async def get_context_prompt(self, user_text: str = "") -> str:
                return "Ты рассказчик анекдотов. Рассказывай смешные анекдоты, учитывай оценки пользователя и старайся угодить его вкусу."

            def __init__(self):
                super().__init__()
                self.count = 0

            @tool("Рассказать анекдот пользователю")
            async def tell_joke(self, text: Annotated[str, "Текст анекдота"]) -> dict:
                self.count += 1
                await self.agent.transport.send_message(f"*Анекдот №{self.count}*\n\n{text}")
                while True:
                    content_parts, _ = await self.agent.next_message()
                    user_text = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict)).strip()
                    if user_text.lower() == "хватит":
                        return {"stop": True}
                    try:
                        rating = int(user_text)
                        if not 1 <= rating <= 10:
                            raise ValueError()
                        return {"rating": rating}
                    except ValueError:
                        await self.agent.transport.send_message("Введи оценку от 1 до 10 или напиши «хватит»")

        sub = await self.agent.spawn_subagent(
            "anecdote_loop",
            memory_providers=[],
            skills=[TellJokeSkill()],
        )
        await sub.memory.add_turn({"role": "user", "content": "Расскажи анекдот."})

        while True:
            tool_calls, _ = await sub.llm(tool_choice="telljoke_tell_joke")
            results = await sub.dispatch_tool_calls(tool_calls)

            if any(r.get("stop") for r in results):
                await self.agent.transport.send_message("Рад был повеселить!")
                return {"status": "завершено"}

