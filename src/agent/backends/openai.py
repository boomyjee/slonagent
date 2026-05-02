"""OpenAIBackend — backend для Agent через OpenAI-совместимый chat.completions."""
import asyncio
import logging
import os

from openai.lib.streaming.chat import ChatCompletionStreamState

from src.agent.agent import Agent, BadFinishReason
from src.agent.backends.base import BaseBackend


class OpenAIBackend(BaseBackend):
    def __init__(self, agent, base_url: str, api_key: str):
        super().__init__(agent)
        self.client = Agent.OpenAI(api_key, base_url)
        self._stream_counter: int = 0

    async def llm(self, tool_choice: str = None, parallel_tool_calls: bool = None, temperature: float = 1.0, max_tokens: int | None = None, system_prompt: str | None = None):
        agent = self.agent
        user_query = agent.memory.last_user_query()
        system_parts = []
        for skill in agent.skills:
            skill_context = await skill.get_context_prompt(user_query)
            if skill_context:
                system_parts.append(skill_context)
                await agent.transport.send_system_prompt(f"[{skill.__class__.__name__}]\n{skill_context}")
        if system_prompt:
            system_parts.append(system_prompt)
        system_instruction = "\n\n".join(system_parts)
        tools = []
        for skill in agent.skills:
            for decl in skill.get_tools():
                fn = decl["function"]
                extra = await skill.get_tool_prompt(fn["name"])
                if extra:
                    decl = {"type": "function", "function": {**fn, "description": fn["description"] + "\n\n---\n" + extra}}
                tools.append(decl)
        if tools:
            tool_lines = [f"{d['function']['name']}: {d['function']['description'].splitlines()[0][:100]}" for d in tools]
            await agent.transport.send_system_prompt("[Tools]\n" + "\n".join(tool_lines))
        contents = agent.strip_contents_private(await agent.memory.get_contents())
        messages = ([{"role": "system", "content": system_instruction}] if system_instruction else []) + contents
        logging.info("[agent] → LLM %s", agent.model_name)
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                kwargs: dict = {"model": agent.model_name, "messages": messages, "stream": True, "temperature": temperature}
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                if tools:
                    kwargs["tools"] = tools
                    if tool_choice in ("auto", "required"):
                        kwargs["tool_choice"] = tool_choice
                    elif tool_choice:
                        kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_choice}}
                    if parallel_tool_calls is not None:
                        kwargs["parallel_tool_calls"] = parallel_tool_calls
                kwargs["extra_body"] = {"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}}

                stream = await self.client.chat.completions.create(**kwargs)

                state = ChatCompletionStreamState()
                display_text = ""
                display_thinking = ""
                self._stream_counter += 1; thinking_id = self._stream_counter
                self._stream_counter += 1; stream_id = self._stream_counter
                is_thought = False  # XML state machine: внутри <thought>...</thought>
                tc_counter = 0
                seen = {}

                async for chunk in stream:
                    # Gemini шлёт role="assistant" в каждом чанке — openai-аккумулятор
                    # склеит в "assistantassistant…", оставляем только из первого.
                    # thought=true приходит в каждом thinking-чанке; на финальной
                    # message он не нужен — Gemini иначе трактует ответ как
                    # внутреннее рассуждение и теряет его в истории.
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is not None:
                        for tc in delta.tool_calls or ():
                            if tc.index is None:
                                tc.index = tc_counter
                                tc_counter += 1
                        if delta.role:
                            if "role" in seen: delta.role = None
                            seen["role"] = True
                        google = ((delta.model_extra or {}).get("extra_content") or {}).get("google") or {}
                        is_thought_chunk = bool(google.pop("thought", None))

                    state.handle_chunk(chunk)
                    if delta is None:
                        continue

                    delta_extra = getattr(delta, "model_extra", None) or {}
                    thought_extra = delta_extra.get("reasoning_content") or delta_extra.get("reasoning")
                    if thought_extra and isinstance(thought_extra, str):
                        display_thinking += thought_extra
                        await agent.transport.send_thinking(display_thinking, thinking_id)

                    # Gemini маркирует мысли двумя сигналами: google.thought=true
                    # flag на чанке ИЛИ литералы <thought>...</thought> в content.
                    # Pro-preview на тяжёлой истории может не ставить flag и не
                    # закрывать </thought> — тогда state machine по XML удерживает
                    # is_thought=True до конца стрима, мысли не утекают в text.
                    # <thought> в content считаем маркером только если он стоит
                    # в самом начале чанка и текст ещё не начался — иначе это
                    # литерал в ответе модели.
                    if content := delta.content or "":
                        if is_thought_chunk or (content.startswith("<thought>") and not display_text):
                            is_thought = True
                        if is_thought:
                            thought, *rest = content.removeprefix("<thought>").split("</thought>", 1)
                            display_thinking += thought
                            if rest:
                                is_thought = False
                                text = rest[0]
                                if text:
                                    display_text += text
                            await agent.transport.send_thinking(display_thinking, thinking_id, final=not is_thought)
                            if not is_thought and display_text:
                                await agent.transport.send_message(display_text, stream_id, final=False)
                        else:
                            display_text += content
                            await agent.transport.send_message(display_text, stream_id, final=False)

                if display_thinking and not display_text:
                    await agent.transport.send_thinking(display_thinking, thinking_id, final=True)
                if display_text:
                    await agent.transport.send_message(display_text, stream_id, final=True)

                final = state.get_final_completion()
                finish_reason = final.choices[0].finish_reason if final.choices else None
                if finish_reason and finish_reason not in ("stop", "tool_calls"):
                    raise BadFinishReason(finish_reason)

                turn = final.choices[0].message.model_dump(exclude_none=True)
                # В content из stream попадают XML-теги <thought>...</thought> —
                # заменяем на очищенный display_text, который мы собрали по флагу.
                turn.pop("content", None)
                if display_text.strip():
                    turn["content"] = display_text.strip()

                for tc in turn.get("tool_calls") or ():
                    tc.pop("index", None)
                    logging.info("[stream] function_call: %s", tc["function"]["name"])

                if not turn.get("content") and not turn.get("tool_calls"):
                    logging.warning("[agent] ← LLM %s returned no content (only thoughts?)", agent.model_name)
                    await agent.transport.send_message("(модель не вернула ответ, только мысли)")

                logging.info("[agent] ← LLM %s finish=%s", agent.model_name, finish_reason)
                return turn
            except BadFinishReason:
                raise
            except Exception as e:
                messages = agent.apply_error_restriction(agent.model_name, e, messages)
                if attempt + 1 == max_retries:
                    raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] LLM %s error, retry %d/%d in %ds: %s", agent.model_name, attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)
